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

    tag_prefix: str = field(default="lm-acceptance", init=False)

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


def load(dotenv_path: str | None = None) -> Config:
    """Load and validate configuration from the environment / .env file."""
    load_dotenv(dotenv_path, override=False)

    token = os.environ.get("DO_API_TOKEN", "").strip()
    if not token:
        raise ConfigError("DO_API_TOKEN is required")

    run_id = os.environ.get("RUN_ID", "").strip() or f"at-{secrets.token_hex(4)}"

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
        microvm_kernel_filename=os.environ.get("MICROVM_KERNEL_FILENAME", "").strip(),
        microvm_namespace=os.environ.get("MICROVM_NAMESPACE", "").strip() or run_id,
        microvm_vcpu=int(os.environ.get("MICROVM_VCPU", "1")),
        microvm_mem_mb=int(os.environ.get("MICROVM_MEM_MB", "512")),
        microvm_count=int(os.environ.get("MICROVM_COUNT", "4")),
        microvm_subnet_cidr=os.environ.get("MICROVM_SUBNET_CIDR", "192.168.100.0/24"),
        brigade_grpc_port=int(os.environ.get("BRIGADE_GRPC_PORT", "9091")),
        brigade_status_port=int(os.environ.get("BRIGADE_STATUS_PORT", "9600")),
        brigade_cookie=os.environ.get("BRIGADE_COOKIE", "").strip() or run_id,
        brigade_min_cluster_size=int(os.environ.get("BRIGADE_MIN_CLUSTER_SIZE", "2")),
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
    )
    return cfg
