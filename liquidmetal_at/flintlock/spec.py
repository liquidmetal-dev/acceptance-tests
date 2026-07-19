"""Build a flintlock MicroVMSpec / CreateMicroVMRequest for the acceptance tests.

Produces a spec with: the configured kernel + rootfs OCI images, a single TAP
interface with a deterministic static IP (so the SSH test knows the target), and
cloud-init user-data (base64) that injects the runner's SSH public key so we can
log into the guest.
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


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def build_spec(cfg: Config, index: int) -> microvm_pb2.MicroVMSpec:
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

    iface = microvm_pb2.NetworkInterface(
        device_id="eth1",
        type=microvm_pb2.NetworkInterface.IfaceType.TAP,
        address=microvm_pb2.StaticAddress(
            address=static_ip, gateway=cfg.microvm_gateway_cidr
        ),
    )
    # flintlock's generated netplan binds the guest NIC by `match:`. With no guest MAC it
    # matches by NAME (device_id, "eth1"); Firecracker's guest names its NIC eth1 (it runs
    # virtio-mmio with `pci=off`, so legacy ethN naming), but Cloud Hypervisor is PCI-based, so
    # the guest enumerates it under predictable naming (e.g. `ens4`) and `match.name: eth1`
    # matches nothing → the NIC never comes up → guest unreachable. Setting guest_mac flips the
    # netplan to `match: {macaddress: ...}`, which is name-agnostic and binds correctly on both
    # providers. Each VM needs a unique, locally-administered (0x02 bit) unicast MAC.
    iface.guest_mac = f"aa:ff:00:00:{(index >> 8) & 0xff:02x}:{index & 0xff:02x}"

    user_data = _cloud_init_user_data(cfg.ssh_public_key, vm_id)
    meta_data = f"instance_id: {vm_id}\nlocal_hostname: {vm_id}\n"

    return microvm_pb2.MicroVMSpec(
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


def build_create_request(cfg: Config, index: int) -> microvms_pb2.CreateMicroVMRequest:
    return microvms_pb2.CreateMicroVMRequest(microvm=build_spec(cfg, index))


def static_ip_of(cfg: Config, index: int) -> str:
    """Bare IP (no CIDR) of the Nth microVM — the SSH target."""
    return cfg.microvm_static_ip(index).split("/")[0]
