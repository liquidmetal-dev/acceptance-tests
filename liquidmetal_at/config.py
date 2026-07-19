"""Environment-driven configuration for the acceptance suite.

Single source of truth for every tunable. Parsed once, validated fail-fast, and
threaded through fixtures. Generates a per-run identity (``RUN_ID``) used as the
DigitalOcean resource tag, the brigade Erlang cookie, and the default microVM
namespace so a run is fully isolated and cleanable.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _expand(p: str) -> str:
    return str(Path(os.path.expanduser(p)).resolve())


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env(name: str, default: str = "") -> str:
    """Env value, treating a python-dotenv inline-comment leak as unset.

    python-dotenv does not strip an inline comment on an *empty-value* line, so a
    ``.env`` line like ``RUN_ID=   # optional`` yields the comment text as the value.
    Such a leak would corrupt DigitalOcean resource names; treat a value starting
    with ``#`` as unset.
    """
    v = os.environ.get(name, default).strip()
    return "" if v.startswith("#") else v


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    # DigitalOcean
    do_token: str
    do_region: str
    do_size: str
    do_image: str
    do_volume_gb: int

    # identity
    run_id: str

    # ssh
    ssh_public_key_path: str
    ssh_private_key_path: str

    # microVM
    microvm_kernel_image: str
    microvm_rootfs_image: str
    microvm_kernel_filename: str
    microvm_namespace: str
    microvm_vcpu: int
    microvm_mem_mb: int
    microvm_count: int
    microvm_subnet_cidr: str

    # brigade / flintlock
    brigade_grpc_port: int
    brigade_status_port: int
    brigade_cookie: str
    brigade_min_cluster_size: int
    brigade_ref: str
    flintlock_ref: str
    flintlock_grpc_port: int

    # timeouts (seconds)
    timeout_provision: int
    timeout_bootstrap: int
    timeout_cluster: int
    timeout_vm_create: int
    timeout_vm_delete: int
    timeout_ssh: int

    # debugging
    keep_infra_on_failure: bool
    artifacts_dir: str

    droplet_count: int = 2

    # hypervisor provider (firecracker | cloudhypervisor). Cloud Hypervisor often
    # needs a PVH-capable kernel, so allow per-provider kernel overrides that fall
    # back to the firecracker kernel image/filename when unset.
    microvm_provider: str = "firecracker"
    microvm_ch_kernel_image: str = ""
    microvm_ch_kernel_filename: str = ""

    tag_prefix: str = field(default="lm-acceptance", init=False)

    @property
    def effective_kernel_image(self) -> str:
        """Kernel OCI image for the selected provider — CH override when set, else base."""
        if self.microvm_provider == "cloudhypervisor" and self.microvm_ch_kernel_image:
            return self.microvm_ch_kernel_image
        return self.microvm_kernel_image

    @property
    def effective_kernel_filename(self) -> str:
        """Kernel path inside the image for the selected provider (CH override when set)."""
        if self.microvm_provider == "cloudhypervisor" and self.microvm_ch_kernel_filename:
            return self.microvm_ch_kernel_filename
        return self.microvm_kernel_filename

    @property
    def tag(self) -> str:
        """DigitalOcean tag applied to every resource this run creates."""
        return f"{self.tag_prefix}-{self.run_id}"

    @property
    def ssh_public_key(self) -> str:
        return Path(self.ssh_public_key_path).read_text().strip()

    def microvm_static_ip(self, index: int) -> str:
        """Deterministic static IP (CIDR) for the Nth microVM on the host bridge."""
        base = self.microvm_subnet_cidr.split("/")[0].rsplit(".", 1)[0]
        prefix = self.microvm_subnet_cidr.split("/")[1]
        return f"{base}.{10 + index}/{prefix}"

    @property
    def microvm_gateway_cidr(self) -> str:
        """Address (CIDR) for the host bridge / guest default gateway, e.g. 192.168.100.1/24."""
        base = self.microvm_subnet_cidr.split("/")[0].rsplit(".", 1)[0]
        prefix = self.microvm_subnet_cidr.split("/")[1]
        return f"{base}.1/{prefix}"

    @property
    def microvm_gateway_ip(self) -> str:
        """Bare gateway IP (no CIDR) assigned to guests' default route."""
        return self.microvm_gateway_cidr.split("/")[0]


def _validate_microvm_shape(mem_mb: int, vcpu: int, kernel_filename: str) -> None:
    """Reject specs flintlock will refuse deep in CreateMicroVM (validate tags on
    core/models: MemoryInMb gte=1024,lte=32768; VCPU gte=1,lte=64; Kernel.Filename required)."""
    if not 1024 <= mem_mb <= 32768:
        raise ConfigError(f"MICROVM_MEM_MB={mem_mb} out of range; flintlock requires 1024-32768")
    if not 1 <= vcpu <= 64:
        raise ConfigError(f"MICROVM_VCPU={vcpu} out of range; flintlock requires 1-64")
    if not kernel_filename:
        raise ConfigError("MICROVM_KERNEL_FILENAME is required by flintlock (e.g. boot/vmlinux)")


_PROVIDERS = ("firecracker", "cloudhypervisor")


def load(dotenv_path: str | None = None) -> Config:
    """Load and validate configuration from the environment / .env file."""
    load_dotenv(dotenv_path, override=False)

    # Drop non-printable/control chars (e.g. a stray ESC from a colorized paste) that a
    # plain .strip() would leave in place and corrupt the Authorization header.
    token = "".join(c for c in os.environ.get("DO_API_TOKEN", "") if c.isprintable()).strip()
    if not token:
        raise ConfigError("DO_API_TOKEN is required")

    run_id = _env("RUN_ID") or f"at-{secrets.token_hex(4)}"

    kernel = os.environ.get("MICROVM_KERNEL_IMAGE", "").strip()
    rootfs = os.environ.get("MICROVM_ROOTFS_IMAGE", "").strip()
    if not kernel or not rootfs:
        raise ConfigError(
            "MICROVM_KERNEL_IMAGE and MICROVM_ROOTFS_IMAGE are required "
            "(supply known-good OCI refs)"
        )

    pub = _expand(os.environ.get("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_ed25519.pub"))
    priv = _expand(os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519"))
    for p, name in ((pub, "SSH_PUBLIC_KEY_PATH"), (priv, "SSH_PRIVATE_KEY_PATH")):
        if not Path(p).is_file():
            raise ConfigError(f"{name} does not exist: {p}")

    # Number of flintlock host droplets == brigade cluster nodes. Quorum target
    # (brigade_min_cluster_size) follows this unless overridden explicitly.
    node_count = int(os.environ.get("NODE_COUNT", "2"))
    if node_count < 1:
        raise ConfigError(f"NODE_COUNT={node_count} must be >= 1")

    provider = (_env("MICROVM_PROVIDER") or "firecracker").lower()
    if provider not in _PROVIDERS:
        raise ConfigError(
            f"MICROVM_PROVIDER={provider!r} invalid; must be one of {_PROVIDERS}"
        )

    cfg = Config(
        do_token=token,
        do_region=os.environ.get("DO_REGION", "nyc3"),
        do_size=os.environ.get("DO_DROPLET_SIZE", "s-4vcpu-8gb"),
        do_image=os.environ.get("DO_IMAGE", "ubuntu-22-04-x64"),
        do_volume_gb=int(os.environ.get("DO_BLOCK_VOLUME_GB", "50")),
        run_id=run_id,
        ssh_public_key_path=pub,
        ssh_private_key_path=priv,
        microvm_kernel_image=kernel,
        microvm_rootfs_image=rootfs,
        # flintlock requires a non-empty Kernel.Filename; boot/vmlinux is the path in the
        # standard liquidmetal kernel images (e.g. firecracker-kernel).
        microvm_kernel_filename=_env("MICROVM_KERNEL_FILENAME") or "boot/vmlinux",
        microvm_namespace=_env("MICROVM_NAMESPACE") or run_id,
        microvm_vcpu=int(os.environ.get("MICROVM_VCPU", "1")),
        # flintlock validates memory_in_mb as gte=1024,lte=32768; 512 is always rejected.
        microvm_mem_mb=int(os.environ.get("MICROVM_MEM_MB", "1024")),
        microvm_count=int(os.environ.get("MICROVM_COUNT", "4")),
        microvm_subnet_cidr=os.environ.get("MICROVM_SUBNET_CIDR", "192.168.100.0/24"),
        brigade_grpc_port=int(os.environ.get("BRIGADE_GRPC_PORT", "9091")),
        brigade_status_port=int(os.environ.get("BRIGADE_STATUS_PORT", "9600")),
        brigade_cookie=_env("BRIGADE_COOKIE") or run_id,
        brigade_min_cluster_size=int(
            os.environ.get("BRIGADE_MIN_CLUSTER_SIZE", str(node_count))
        ),
        brigade_ref=os.environ.get("BRIGADE_REF", "main"),
        flintlock_ref=os.environ.get("FLINTLOCK_REF", "main"),
        flintlock_grpc_port=int(os.environ.get("FLINTLOCK_GRPC_PORT", "9090")),
        timeout_provision=int(os.environ.get("TIMEOUT_PROVISION", "300")),
        timeout_bootstrap=int(os.environ.get("TIMEOUT_BOOTSTRAP", "1200")),
        timeout_cluster=int(os.environ.get("TIMEOUT_CLUSTER", "180")),
        timeout_vm_create=int(os.environ.get("TIMEOUT_VM_CREATE", "300")),
        timeout_vm_delete=int(os.environ.get("TIMEOUT_VM_DELETE", "120")),
        timeout_ssh=int(os.environ.get("TIMEOUT_SSH", "180")),
        keep_infra_on_failure=_bool(os.environ.get("KEEP_INFRA_ON_FAILURE", "false")),
        artifacts_dir=_expand(os.environ.get("ARTIFACTS_DIR", "./artifacts")),
        droplet_count=node_count,
        microvm_provider=provider,
        microvm_ch_kernel_image=_env("MICROVM_CH_KERNEL_IMAGE"),
        microvm_ch_kernel_filename=_env("MICROVM_CH_KERNEL_FILENAME"),
    )
    _validate_microvm_shape(cfg.microvm_mem_mb, cfg.microvm_vcpu, cfg.microvm_kernel_filename)
    return cfg
