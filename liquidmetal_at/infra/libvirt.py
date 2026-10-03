"""Local KVM provisioning + name-scoped teardown via system libvirt.

Provisions, for one acceptance run: a NAT network and one VM per flintlock host, each a
qcow2 overlay on a shared, cached Ubuntu cloud image. Everything is driven by shelling
out to ``virsh`` / ``virt-install`` through one injectable ``run`` callable, so the logic
is testable without a hypervisor. Every per-run object is named after ``cfg.tag`` so
``destroy_run`` can reap a run even if the process died before returning an ``Infra``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from ..config import DEFAULT_LIBVIRT_POOL, DEFAULT_LIBVIRT_URI, Config

log = logging.getLogger("infra.libvirt")

PREFIX = "lm-acceptance-"
# Shared base images. Never deleted by teardown or sweep.
BASE_PREFIX = "lm-acceptance-base-"
POOL_ROOT = "/var/lib/libvirt/images"
CACHE_DIR = Path("~/.cache/lm-acceptance").expanduser()
KVM_DEVICE = Path("/dev/kvm")
NESTED_PARAMS = (
    Path("/sys/module/kvm_intel/parameters/nested"),
    Path("/sys/module/kvm_amd/parameters/nested"),
)
DOCS = "docs/libvirt-backend.md"

Runner = Callable[[list[str]], str]


class LibvirtError(RuntimeError):
    """A virsh/virt-install command failed, or the host cannot run the libvirt backend."""


def _exec(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise LibvirtError(
            f"{' '.join(argv)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _virsh(uri: str, run: Runner, *args: str) -> str:
    return run(["virsh", "-c", uri, *args])


def _volumes(uri: str, pool: str, run: Runner) -> list[str]:
    """Volume names in ``pool`` (virsh vol-list has no --name; skip the 2 header lines)."""
    lines = _virsh(uri, run, "vol-list", pool).splitlines()
    return [ln.split()[0] for ln in lines[2:] if ln.strip()]


# --- preflight ---------------------------------------------------------------


def _nested_enabled() -> bool:
    for param in NESTED_PARAMS:
        if param.is_file() and param.read_text().strip() in ("1", "Y", "y"):
            return True
    return False


def preflight(cfg: Config, run: Runner = _exec) -> None:
    """Fail fast, with a fixable message, if this host cannot run the libvirt backend."""
    for tool in ("virsh", "virt-install"):
        if not shutil.which(tool):
            raise LibvirtError(
                f"{tool} not found on PATH; install libvirt + virt-install ({DOCS})"
            )
    if not KVM_DEVICE.exists():
        raise LibvirtError(f"{KVM_DEVICE} not present; KVM is required ({DOCS})")
    if not _nested_enabled():
        raise LibvirtError(
            "nested virtualization is disabled; flintlock hosts need /dev/kvm inside the VM. "
            f"Enable kvm_intel/kvm_amd nested=1 ({DOCS})"
        )
    try:
        _virsh(cfg.libvirt_uri, run, "version")
    except LibvirtError as exc:
        raise LibvirtError(
            f"cannot reach libvirt at {cfg.libvirt_uri}: {exc}. Is the daemon running and "
            f"is this user in the libvirt group? ({DOCS})"
        ) from exc


# --- storage pool + base image ----------------------------------------------


def ensure_pool(cfg: Config, run: Runner = _exec) -> None:
    uri, pool = cfg.libvirt_uri, cfg.libvirt_pool
    if pool not in _virsh(uri, run, "pool-list", "--all", "--name").split():
        log.info("creating libvirt storage pool %s", pool)
        _virsh(uri, run, "pool-define-as", pool, "dir", "--target", f"{POOL_ROOT}/{pool}")
        _virsh(uri, run, "pool-build", pool)
    if pool not in _virsh(uri, run, "pool-list", "--name").split():
        _virsh(uri, run, "pool-start", pool)


def pool_path(cfg: Config, run: Runner = _exec) -> str:
    xml = _virsh(cfg.libvirt_uri, run, "pool-dumpxml", cfg.libvirt_pool)
    path = ET.fromstring(xml).findtext("target/path")
    if not path:
        raise LibvirtError(f"pool {cfg.libvirt_pool} has no target path")
    return path


def base_volume_name(cfg: Config) -> str:
    """Volume name for the pinned image: release date (when the URL has one) + checksum."""
    m = re.search(r"release-(\d{8})", cfg.libvirt_base_image_url)
    date = m.group(1) if m else "undated"
    return f"{BASE_PREFIX}{date}-{cfg.libvirt_base_image_sha256[:8]}.qcow2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_base(cfg: Config) -> Path:
    """Fetch the pinned image into the cache (temp file + atomic rename), verifying it."""
    want = cfg.libvirt_base_image_sha256
    dest = CACHE_DIR / f"{want[:16]}.img"
    if dest.is_file() and _sha256(dest) == want:
        return dest
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".part")
    os.close(fd)
    try:
        log.info("downloading base image %s", cfg.libvirt_base_image_url)
        urllib.request.urlretrieve(cfg.libvirt_base_image_url, tmp)
        got = _sha256(Path(tmp))
        if got != want:
            raise LibvirtError(
                f"checksum mismatch for {cfg.libvirt_base_image_url}: "
                f"expected {want}, got {got}"
            )
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return dest


def ensure_base_image(cfg: Config, run: Runner = _exec) -> str:
    """Make sure the pinned base image exists as a pool volume; return its name."""
    uri, pool = cfg.libvirt_uri, cfg.libvirt_pool
    name = base_volume_name(cfg)
    if name in _volumes(uri, pool, run):
        return name
    image = _download_base(cfg)
    log.info("uploading base image into pool %s as %s", pool, name)
    _virsh(uri, run, "vol-create-as", pool, name, str(image.stat().st_size), "--format", "qcow2")
    try:
        _virsh(uri, run, "vol-upload", "--pool", pool, name, str(image))
    except LibvirtError:
        # A half-uploaded base would be trusted by every later run; remove it.
        _virsh(uri, run, "vol-delete", "--pool", pool, name)
        raise
    return name


# --- teardown ----------------------------------------------------------------


def _virsh_quiet(uri: str, run: Runner, *args: str) -> None:
    """Run a virsh command whose failure is expected and harmless (e.g. already stopped)."""
    try:
        _virsh(uri, run, *args)
    except LibvirtError as exc:
        log.debug("ignored: %s", exc)


def _destroy_matching(uri: str, pool: str, match: Callable[[str], bool], run: Runner) -> None:
    """Remove every domain, non-base volume and network whose name satisfies ``match``.

    Order: domains first (they hold the volumes and network open), then volumes, then
    networks. Safe to call twice.
    """
    for dom in _virsh(uri, run, "list", "--all", "--name").split():
        if match(dom):
            _virsh_quiet(uri, run, "destroy", dom)  # fails if not running
            _virsh(uri, run, "undefine", dom, "--nvram")
    if pool in _virsh(uri, run, "pool-list", "--name").split():
        _virsh(uri, run, "pool-refresh", pool)  # console logs are created by QEMU, not virsh
        for vol in _volumes(uri, pool, run):
            if match(vol) and not vol.startswith(BASE_PREFIX):
                _virsh(uri, run, "vol-delete", "--pool", pool, vol)
    for net in _virsh(uri, run, "net-list", "--all", "--name").split():
        if match(net):
            _virsh_quiet(uri, run, "net-destroy", net)  # fails if not active
            _virsh(uri, run, "net-undefine", net)


def _belongs_to(tag: str) -> Callable[[str], bool]:
    """Exact-run matcher: ``lm-acceptance-at-1`` must not match ``lm-acceptance-at-10``."""
    return lambda name: name == tag or name.startswith(f"{tag}-host")


def destroy_run(cfg: Config, run: Runner = _exec) -> None:
    """Idempotently delete every libvirt object belonging to this run."""
    log.info("tearing down libvirt infra %s", cfg.tag)
    _destroy_matching(cfg.libvirt_uri, cfg.libvirt_pool, _belongs_to(cfg.tag), run)


def sweep(uri: str, pool: str, run: Runner = _exec) -> None:
    """Delete every lm-acceptance-* domain, network and volume except base images."""
    log.info("sweeping all %s* libvirt resources at %s", PREFIX, uri)
    _destroy_matching(uri, pool, lambda name: name.startswith(PREFIX), run)


# --- diagnostics -------------------------------------------------------------


def collect_consoles(cfg: Config, run: Runner = _exec) -> None:
    """Save each VM's serial console log to the artifacts dir. Best effort: never raises.

    The console is the only evidence when a VM never boots or never gets an address,
    because every other log is collected over SSH.
    """
    uri, pool = cfg.libvirt_uri, cfg.libvirt_pool
    out = Path(cfg.artifacts_dir) / cfg.run_id
    try:
        if pool not in _virsh(uri, run, "pool-list", "--name").split():
            return
        out.mkdir(parents=True, exist_ok=True)
        _virsh(uri, run, "pool-refresh", pool)
        prefix = f"{cfg.tag}-"
        for vol in _volumes(uri, pool, run):
            if _belongs_to(cfg.tag)(vol) and vol.endswith("-console.log"):
                dest = out / vol.removeprefix(prefix)
                _virsh(uri, run, "vol-download", "--pool", pool, vol, str(dest))
        log.info("collected VM console logs into %s", out)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the real failure
        log.warning("could not collect VM console logs: %s", exc)


# --- sweep CLI (make clean-libvirt) -------------------------------------------


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    uri = os.environ.get("LIBVIRT_URI", "").strip() or DEFAULT_LIBVIRT_URI
    pool = os.environ.get("LIBVIRT_POOL", "").strip() or DEFAULT_LIBVIRT_POOL
    try:
        sweep(uri, pool)
    except LibvirtError as exc:
        log.error("sweep failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
