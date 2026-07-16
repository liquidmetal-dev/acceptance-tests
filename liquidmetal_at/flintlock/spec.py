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

    kernel = microvm_pb2.Kernel(image=cfg.microvm_kernel_image, add_network_config=True)
    if cfg.microvm_kernel_filename:
        kernel.filename = cfg.microvm_kernel_filename

    root_volume = microvm_pb2.Volume(
        id="root",
        is_read_only=False,
        source=microvm_pb2.VolumeSource(container_source=cfg.microvm_rootfs_image),
    )

    iface = microvm_pb2.NetworkInterface(
        device_id="eth1",
        type=microvm_pb2.NetworkInterface.IfaceType.TAP,
        address=microvm_pb2.StaticAddress(address=static_ip),
    )

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
    )


def build_create_request(cfg: Config, index: int) -> microvms_pb2.CreateMicroVMRequest:
    return microvms_pb2.CreateMicroVMRequest(microvm=build_spec(cfg, index))


def static_ip_of(cfg: Config, index: int) -> str:
    """Bare IP (no CIDR) of the Nth microVM — the SSH target."""
    return cfg.microvm_static_ip(index).split("/")[0]
