"""Local KVM provisioning + name-scoped teardown via system libvirt.

Provisions, for one acceptance run: a NAT network and one VM per flintlock host, each a
qcow2 overlay on a shared, cached Ubuntu cloud image. Everything is driven by shelling
out to ``virsh`` / ``virt-install`` through one injectable ``run`` callable, so the logic
is testable without a hypervisor. Every per-run object is named after ``cfg.tag`` so
``destroy_run`` can reap a run even if the process died before returning an ``Infra``.
"""
from __future__ import annotations

import fcntl
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
from ..remote.ssh import SSH
from ..waiter import WaitTimeout, wait_until
from .types import Infra, Node

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
OSINFO = "ubuntu22.04"  # --import cannot detect the OS; virt-install requires it
LEASE_TIMEOUT = 120  # seconds for a VM to obtain its DHCP lease
SUBNET_ATTEMPTS = 5

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
    """Make sure the pinned base image exists as a pool volume; return its name.

    The volume is only trusted once its ``.ok`` marker exists: the marker is created
    after the upload completes, so a run killed mid-upload (Ctrl-C, cancelled CI job)
    leaves an unmarked volume that the next run replaces instead of booting from. A
    file lock keeps two runs starting together from uploading over each other.
    """
    uri, pool = cfg.libvirt_uri, cfg.libvirt_pool
    name = base_volume_name(cfg)
    marker = f"{name}.ok"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with (CACHE_DIR / "base-image.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = _volumes(uri, pool, run)
        if name in existing and marker in existing:
            return name
        if name in existing:
            log.warning("base volume %s is incomplete (no %s); replacing it", name, marker)
            _virsh(uri, run, "vol-delete", "--pool", pool, name)
        image = _download_base(cfg)
        log.info("uploading base image into pool %s as %s", pool, name)
        size = str(image.stat().st_size)
        _virsh(uri, run, "vol-create-as", pool, name, size, "--format", "qcow2")
        try:
            _virsh(uri, run, "vol-upload", "--pool", pool, name, str(image))
        except LibvirtError:
            _virsh(uri, run, "vol-delete", "--pool", pool, name)
            raise
        _virsh(uri, run, "vol-create-as", pool, marker, "4096", "--format", "raw")
    return name


# --- network -----------------------------------------------------------------


def node_name(cfg: Config, index: int) -> str:
    return f"{cfg.tag}-host{index}"


def _ip_for(subnet: str, index: int) -> str:
    return f"{subnet}.{10 + index}"


def _mac_for(subnet: str, index: int) -> str:
    """Stable MAC per (subnet, node) so the DHCP host entry pins the address."""
    third = int(subnet.rsplit(".", 1)[1])
    return f"52:54:00:d2:{third:02x}:{10 + index:02x}"


def network_xml(cfg: Config, subnet: str) -> str:
    """NAT network ``<subnet>.0/24`` with a fixed DHCP lease per node."""
    hosts = "\n".join(
        f"      <host mac='{_mac_for(subnet, i)}' name='{node_name(cfg, i)}' "
        f"ip='{_ip_for(subnet, i)}'/>"
        for i in range(cfg.node_count)
    )
    return (
        "<network>\n"
        f"  <name>{cfg.tag}</name>\n"
        "  <forward mode='nat'/>\n"
        f"  <ip address='{subnet}.1' netmask='255.255.255.0'>\n"
        "    <dhcp>\n"
        f"      <range start='{subnet}.100' end='{subnet}.200'/>\n"
        f"{hosts}\n"
        "    </dhcp>\n"
        "  </ip>\n"
        "</network>\n"
    )


def _used_subnets(cfg: Config, run: Runner) -> set[str]:
    used: set[str] = set()
    for net in _virsh(cfg.libvirt_uri, run, "net-list", "--all", "--name").split():
        xml = _virsh(cfg.libvirt_uri, run, "net-dumpxml", net)
        for ip in ET.fromstring(xml).findall("ip"):
            used.add(ip.get("address", "").rsplit(".", 1)[0])
    return used


def pick_subnet(cfg: Config, run: Runner, skip: frozenset[str] = frozenset()) -> str:
    """First ``<prefix>.N`` (a /24) no libvirt network uses and that isn't in ``skip``."""
    used = _used_subnets(cfg, run) | skip
    for n in range(256):
        candidate = f"{cfg.libvirt_subnet_prefix}.{n}"
        if candidate not in used:
            return candidate
    raise LibvirtError(f"no free /24 under {cfg.libvirt_subnet_prefix}.0.0/16")


def create_network(cfg: Config, run: Runner = _exec) -> str:
    """Define + start this run's NAT network; return its subnet (``a.b.c``).

    Starting can fail when the range is in use by something libvirt doesn't list (a
    concurrent run that picked it first, a VPN); move on to the next free range.
    """
    uri = cfg.libvirt_uri
    tried: set[str] = set()
    last: LibvirtError | None = None
    for _ in range(SUBNET_ATTEMPTS):
        subnet = pick_subnet(cfg, run, frozenset(tried))
        tried.add(subnet)
        with tempfile.NamedTemporaryFile("w", suffix=".xml") as fh:
            fh.write(network_xml(cfg, subnet))
            fh.flush()
            _virsh(uri, run, "net-define", fh.name)
        try:
            _virsh(uri, run, "net-start", cfg.tag)
        except LibvirtError as exc:
            log.warning("subnet %s.0/24 unusable (%s); trying the next one", subnet, exc)
            last = exc
            _virsh(uri, run, "net-undefine", cfg.tag)
            continue
        log.info("created network %s on %s.0/24", cfg.tag, subnet)
        return subnet
    raise LibvirtError(f"could not start network {cfg.tag}: {last}")


# --- VMs ---------------------------------------------------------------------


def _create_vm(
    cfg: Config, run: Runner, index: int, subnet: str, base: str, pool_dir: str, user_data: str
) -> Node:
    uri, pool = cfg.libvirt_uri, cfg.libvirt_pool
    name = node_name(cfg, index)
    _virsh(
        uri, run, "vol-create-as", pool, f"{name}.qcow2", f"{cfg.libvirt_disk_gb}G",
        "--format", "qcow2", "--backing-vol", base, "--backing-vol-format", "qcow2",
    )
    with tempfile.TemporaryDirectory() as tmp:
        ud, md = Path(tmp) / "user-data", Path(tmp) / "meta-data"
        ud.write_text(user_data)
        md.write_text(f"instance-id: {name}\nlocal-hostname: {name}\n")
        run([
            "virt-install", "--connect", uri,
            "--name", name,
            "--memory", str(cfg.libvirt_memory_mb),
            "--vcpus", str(cfg.libvirt_vcpus),
            "--cpu", "host-passthrough",  # exposes svm/vmx so the guest gets /dev/kvm
            "--import",
            "--disk", f"vol={pool}/{name}.qcow2,bus=virtio",
            "--network", f"network={cfg.tag},mac={_mac_for(subnet, index)},model=virtio",
            "--cloud-init", f"user-data={ud},meta-data={md}",
            "--serial", f"file,path={pool_dir}/{name}-console.log",
            "--graphics", "none",
            "--noautoconsole",
            "--osinfo", OSINFO,
        ])
    ip = _ip_for(subnet, index)
    log.info("created VM %s at %s", name, ip)
    return Node(name=name, public_ip=ip, private_ip=ip, parent_iface=None)


def _wait_lease(cfg: Config, run: Runner, subnet: str, index: int) -> None:
    mac = _mac_for(subnet, index)
    name = node_name(cfg, index)
    try:
        wait_until(
            lambda: mac in _virsh(cfg.libvirt_uri, run, "net-dhcp-leases", cfg.tag),
            timeout=LEASE_TIMEOUT,
            interval=3,
            description=f"DHCP lease for {name}",
        )
    except WaitTimeout as exc:
        raise LibvirtError(
            f"{name} got no DHCP lease on network {cfg.tag} within {LEASE_TIMEOUT}s. "
            "A host firewall is probably dropping DHCP/DNS on the libvirt bridge: with ufw "
            'or another default-deny firewall, set firewall_backend = "iptables" in '
            f"/etc/libvirt/network.conf and restart libvirt ({DOCS})"
        ) from exc


def _wait_ssh(cfg: Config, ip: str) -> None:
    ssh = SSH(host=ip, user="root", key_path=cfg.ssh_private_key_path)
    ssh.connect(timeout=cfg.timeout_provision)
    ssh.close()


# --- public API --------------------------------------------------------------


def provision(
    cfg: Config, user_data_for: Callable[[int, str], str], run: Runner = _exec
) -> Infra:
    """Provision all infra for a run. ``user_data_for(index, name)`` -> cloud-init str."""
    log.info("provisioning libvirt infra %s at %s", cfg.tag, cfg.libvirt_uri)
    preflight(cfg, run)
    ensure_pool(cfg, run)
    base = ensure_base_image(cfg, run)
    pool_dir = pool_path(cfg, run)

    infra = Infra(cfg=cfg)
    try:
        subnet = create_network(cfg, run)
        for i in range(cfg.node_count):
            user_data = user_data_for(i, node_name(cfg, i))
            infra.nodes.append(_create_vm(cfg, run, i, subnet, base, pool_dir, user_data))
        for i, node in enumerate(infra.nodes):
            _wait_lease(cfg, run, subnet, i)
            _wait_ssh(cfg, node.public_ip)
            log.info("VM %s reachable over SSH", node.name)
    except BaseException:
        # Unlike DO there is no out-of-band reaper watching this machine, so a failed
        # provision cleans up after itself — after saving the only evidence there is.
        # BaseException: Ctrl-C or a pytest timeout during the SSH wait must clean up too,
        # since the fixture has not yielded and pytest will run no teardown.
        collect_consoles(cfg, run)
        if cfg.keep_infra_on_failure:
            log.warning("KEEP_INFRA_ON_FAILURE set - leaving %s up (make clean-libvirt)", cfg.tag)
        else:
            destroy_run(cfg, run)
        raise
    return infra


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
