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
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from ..config import Config

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
