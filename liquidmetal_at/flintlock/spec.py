"""Build a flintlock MicroVMSpec / CreateMicroVMRequest for the acceptance tests.

Produces a spec with: the configured kernel + rootfs OCI images, a single TAP
interface with a deterministic static IP (so the SSH test knows the target), and
cloud-init user-data (base64) that injects the runner's SSH public key so we can
log into the guest.

With ``guest_agent=True`` the spec additionally opts in to the vsock guest-agent
(``allow_guest_agent``), adds nameservers to the NIC, and swaps in cloud-init that
installs + starts the guest-agent — see :mod:`tests.test_guest_agent_vsock`.
"""
from __future__ import annotations

import base64

from flapi import microvms_pb2
from fltypes import microvm_pb2

from ..config import Config


def _cloud_init_user_data(ssh_pubkey: str, hostname: str) -> str:
    return (
        "#cloud-config\n"
        "hostname: " + hostname + "\n"
        "users:\n"
        "  - name: root\n"
        "    ssh_authorized_keys:\n"
        f"      - {ssh_pubkey}\n"
        "disable_root: false\n"
        "ssh_pwauth: false\n"
    )


# The signed LiquidMetal apt repo that ships the guest-agent package.
_GA_KEYRING = "/usr/share/keyrings/liquidmetal-archive-keyring.gpg"
_GA_KEY_URL = "https://liquidmetal-dev.github.io/apt-repo/liquidmetal-archive-keyring.asc"
_GA_REPO = "https://liquidmetal-dev.github.io/apt-repo"


def _cloud_init_guest_agent(ssh_pubkey: str, hostname: str) -> str:
    """Like :func:`_cloud_init_user_data` but also installs + starts the guest-agent.

    Unlike the SSH-reachability guest (which pointedly avoids DNS), this guest fetches the
    guest-agent from the LiquidMetal apt repo, so it needs name resolution. We add a
    nameserver to /etc/resolv.conf up front (belt-and-suspenders with the netplan nameservers
    set on the interface — systemd-resolved may rewrite resolv.conf) and run everything in
    ``runcmd`` so it fires late, after the NIC + default route are up. ``curl``/``gpg`` are
    installed first in case the rootfs lacks them. The guest-agent ``.deb`` ships a systemd
    unit; we enable it so it listens on vsock (control port 1024) at boot.
    """
    deb_line = f"deb [signed-by={_GA_KEYRING}] {_GA_REPO} stable main"
    return (
        "#cloud-config\n"
        "hostname: " + hostname + "\n"
        "users:\n"
        "  - name: root\n"
        "    ssh_authorized_keys:\n"
        f"      - {ssh_pubkey}\n"
        "disable_root: false\n"
        "ssh_pwauth: false\n"
        "runcmd:\n"
        "  - [ sh, -c, 'echo nameserver 1.1.1.1 >> /etc/resolv.conf' ]\n"
        "  - [ sh, -c, 'apt-get update && "
        "apt-get install -y curl gpg ca-certificates' ]\n"
        "  - [ sh, -c, 'install -d -m 0755 /usr/share/keyrings' ]\n"
        f"  - [ sh, -c, 'curl -fsSL {_GA_KEY_URL} | gpg --dearmor -o {_GA_KEYRING}' ]\n"
        f"  - [ sh, -c, 'echo \"{deb_line}\" > /etc/apt/sources.list.d/liquidmetal.list' ]\n"
        "  - [ sh, -c, 'apt-get update && apt-get install -y guest-agent' ]\n"
        "  - [ systemctl, enable, --now, guest-agent.service ]\n"
    )


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def build_spec(
    cfg: Config, index: int, *, guest_agent: bool = False
) -> microvm_pb2.MicroVMSpec:
    vm_id = f"{cfg.run_id}-vm{index}"
    static_ip = cfg.microvm_static_ip(index)

    kernel = microvm_pb2.Kernel(image=cfg.effective_kernel_image, add_network_config=True)
    if cfg.effective_kernel_filename:
        kernel.filename = cfg.effective_kernel_filename
    # Cloud Hypervisor delivers cloud-init (incl. the netplan network-config) ONLY via the
    # FAT32 `cidata` NoCloud disk — unlike Firecracker, its default kernel cmdline carries no
    # `ds=` datasource hint and there is no MMDS metadata service. Without a hint the guest's
    # cloud-init may not probe the cidata disk, so the NIC never comes up and the guest is
    # unreachable ("No route to host"). Force the NoCloud local datasource for CH guests.
    if cfg.microvm_provider == "cloudhypervisor":
        kernel.cmdline["ds"] = "nocloud"

    root_volume = microvm_pb2.Volume(
        id="root",
        is_read_only=False,
        source=microvm_pb2.VolumeSource(container_source=cfg.microvm_rootfs_image),
    )

    addr = microvm_pb2.StaticAddress(address=static_ip, gateway=cfg.microvm_gateway_cidr)
    if guest_agent:
        # The guest-agent install pulls from the LiquidMetal apt repo over the network, so the
        # guest needs a resolver. Set it in the netplan flintlock generates (the durable path;
        # resolv.conf gets a fallback too, see _cloud_init_guest_agent).
        addr.nameservers.extend(["1.1.1.1", "8.8.8.8"])
    iface = microvm_pb2.NetworkInterface(
        device_id="eth1",
        type=microvm_pb2.NetworkInterface.IfaceType.TAP,
        address=addr,
    )
    # flintlock's generated netplan binds the guest NIC by `match:`. With no guest MAC it
    # matches by NAME (device_id, "eth1"); Firecracker's guest names its NIC eth1 (it runs
    # virtio-mmio with `pci=off`, so legacy ethN naming), but Cloud Hypervisor is PCI-based, so
    # the guest enumerates it under predictable naming (e.g. `ens4`) and `match.name: eth1`
    # matches nothing → the NIC never comes up → guest unreachable. Setting guest_mac flips the
    # netplan to `match: {macaddress: ...}`, which is name-agnostic and binds correctly on both
    # providers. Each VM needs a unique, locally-administered (0x02 bit) unicast MAC.
    iface.guest_mac = f"aa:ff:00:00:{(index >> 8) & 0xff:02x}:{index & 0xff:02x}"

    if guest_agent:
        user_data = _cloud_init_guest_agent(cfg.ssh_public_key, vm_id)
    else:
        user_data = _cloud_init_user_data(cfg.ssh_public_key, vm_id)
    meta_data = f"instance_id: {vm_id}\nlocal_hostname: {vm_id}\n"

    vm_spec = microvm_pb2.MicroVMSpec(
        id=vm_id,
        namespace=cfg.microvm_namespace,
        vcpu=cfg.microvm_vcpu,
        memory_in_mb=cfg.microvm_mem_mb,
        kernel=kernel,
        root_volume=root_volume,
        interfaces=[iface],
        metadata={"user-data": _b64(user_data), "meta-data": _b64(meta_data)},
        provider=cfg.microvm_provider,
    )
    if guest_agent:
        # allow_guest_agent (proto field 17) tells flintlock to attach the vsock device and
        # report the host-side socket in status.vsock_path. It only exists in stubs regenerated
        # from flintlock >= v0.11.0 — fail loudly if the stubs are stale rather than silently
        # dropping the flag and creating a VM with no guest-agent transport.
        if "allow_guest_agent" not in {f.name for f in vm_spec.DESCRIPTOR.fields}:
            raise RuntimeError(
                "MicroVMSpec has no allow_guest_agent field; regenerate stubs with "
                "'FLINTLOCK_REF=v0.11.0 make refresh-proto && make proto'"
            )
        vm_spec.allow_guest_agent = True
    return vm_spec


def build_create_request(
    cfg: Config, index: int, *, guest_agent: bool = False
) -> microvms_pb2.CreateMicroVMRequest:
    return microvms_pb2.CreateMicroVMRequest(
        microvm=build_spec(cfg, index, guest_agent=guest_agent)
    )


def static_ip_of(cfg: Config, index: int) -> str:
    """Bare IP (no CIDR) of the Nth microVM — the SSH target."""
    return cfg.microvm_static_ip(index).split("/")[0]
