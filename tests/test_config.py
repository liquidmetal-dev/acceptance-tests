"""Offline checks for config parsing.

No DigitalOcean token or infra needed. Guards the ``.env`` inline-comment footgun:
python-dotenv leaves an inline comment on an empty-value line as the value, which
must not leak into DigitalOcean resource names (which allow only ``[A-Za-z0-9.-]``).
"""
from __future__ import annotations

import re

import pytest

from liquidmetal_at.config import ConfigError, _env, load

# python-dotenv yields the whole comment as the value for `RUN_ID=   # optional ...`.
COMMENT_LEAK = "# optional; auto-generated as at-<8hex> if empty"
DO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")


def test_env_treats_inline_comment_leak_as_unset(monkeypatch):
    monkeypatch.setenv("LEAK", COMMENT_LEAK)
    monkeypatch.setenv("REAL", "at-cafef00d")
    monkeypatch.delenv("MISSING", raising=False)

    assert _env("LEAK") == ""
    assert _env("REAL") == "at-cafef00d"
    assert _env("MISSING") == ""
    assert _env("MISSING", "fallback") == "fallback"


def _prime_required(monkeypatch, tmp_path):
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAATESTKEY test@runner")
    priv.write_text("PRIVATE")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    # Isolate from the caller's environment: `INFRA_BACKEND=libvirt make test` runs these too.
    for var in ("INFRA_BACKEND", "RUN_ID", "LIBVIRT_URI", "LIBVIRT_POOL", "LIBVIRT_VCPUS",
                "LIBVIRT_MEMORY_MB", "LIBVIRT_DISK_GB", "LIBVIRT_SUBNET_PREFIX",
                "LIBVIRT_BASE_IMAGE_URL", "LIBVIRT_BASE_IMAGE_SHA256"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DO_API_TOKEN", "dummy-token")
    monkeypatch.setenv("MICROVM_KERNEL_IMAGE", "ghcr.io/example/kernel:5.10")
    monkeypatch.setenv("MICROVM_ROOTFS_IMAGE", "ghcr.io/example/rootfs:1.0")
    monkeypatch.setenv("SSH_PUBLIC_KEY_PATH", str(pub))
    monkeypatch.setenv("SSH_PRIVATE_KEY_PATH", str(priv))
    return str(empty_env)


def test_run_id_leak_falls_back_to_generated_and_yields_valid_tag(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("RUN_ID", COMMENT_LEAK)
    monkeypatch.setenv("MICROVM_NAMESPACE", COMMENT_LEAK)
    monkeypatch.setenv("BRIGADE_COOKIE", COMMENT_LEAK)

    cfg = load(dotenv_path=empty_env)

    assert re.fullmatch(r"at-[0-9a-f]{8}", cfg.run_id), cfg.run_id
    assert cfg.microvm_namespace == cfg.run_id
    assert cfg.brigade_cookie == cfg.run_id
    # The DO VPC name is cfg.tag — must contain only DO-legal characters.
    assert DO_NAME_RE.fullmatch(cfg.tag), cfg.tag


def test_provider_defaults_to_firecracker(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.delenv("MICROVM_PROVIDER", raising=False)

    cfg = load(dotenv_path=empty_env)

    assert cfg.microvm_provider == "firecracker"
    # Without a provider switch, effective kernel == the base firecracker kernel.
    assert cfg.effective_kernel_image == cfg.microvm_kernel_image
    assert cfg.effective_kernel_filename == cfg.microvm_kernel_filename


def test_provider_cloudhypervisor_uses_ch_kernel_overrides(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("MICROVM_PROVIDER", "CloudHypervisor")  # case-insensitive
    monkeypatch.setenv("MICROVM_CH_KERNEL_IMAGE", "ghcr.io/example/ch-kernel:6.1")
    monkeypatch.setenv("MICROVM_CH_KERNEL_FILENAME", "boot/ch-vmlinux")

    cfg = load(dotenv_path=empty_env)

    assert cfg.microvm_provider == "cloudhypervisor"
    assert cfg.effective_kernel_image == "ghcr.io/example/ch-kernel:6.1"
    assert cfg.effective_kernel_filename == "boot/ch-vmlinux"


def test_provider_cloudhypervisor_falls_back_to_base_kernel(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("MICROVM_PROVIDER", "cloudhypervisor")
    monkeypatch.delenv("MICROVM_CH_KERNEL_IMAGE", raising=False)
    monkeypatch.delenv("MICROVM_CH_KERNEL_FILENAME", raising=False)

    cfg = load(dotenv_path=empty_env)

    # No CH overrides → reuse the firecracker kernel image/filename.
    assert cfg.effective_kernel_image == cfg.microvm_kernel_image
    assert cfg.effective_kernel_filename == cfg.microvm_kernel_filename


def test_invalid_provider_raises(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("MICROVM_PROVIDER", "qemu")

    with pytest.raises(ConfigError):
        load(dotenv_path=empty_env)


def test_invalid_battery_log_level_raises(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("BATTERY_LOG_LEVEL", "trace")

    with pytest.raises(ConfigError):
        load(dotenv_path=empty_env)


def test_battery_defaults(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    for var in (
        "BATTERY_REF",
        "BATTERY_API_PORT",
        "BATTERY_METRICS_PORT",
        "BATTERY_SWEEP_INTERVAL",
        "BATTERY_WARNING_WINDOW",
        "BATTERY_LOG_LEVEL",
        "TIMEOUT_POOL_AVAILABLE",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = load(dotenv_path=empty_env)

    assert cfg.battery_ref == "v0.3.2"
    assert cfg.battery_api_port == 9191
    assert cfg.battery_metrics_port == 9192
    assert cfg.battery_sweep_interval == "10s"
    assert cfg.battery_warning_window == "5s"
    assert cfg.battery_log_level == "debug"
    assert cfg.timeout_pool_available == 300


def test_battery_overrides(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("BATTERY_REF", "v0.2.0")
    monkeypatch.setenv("BATTERY_API_PORT", "9291")
    monkeypatch.setenv("BATTERY_METRICS_PORT", "9292")
    monkeypatch.setenv("BATTERY_SWEEP_INTERVAL", "1s")
    monkeypatch.setenv("BATTERY_WARNING_WINDOW", "1s")
    monkeypatch.setenv("BATTERY_LOG_LEVEL", "WARN")
    monkeypatch.setenv("TIMEOUT_POOL_AVAILABLE", "600")

    cfg = load(dotenv_path=empty_env)

    assert cfg.battery_ref == "v0.2.0"
    assert cfg.battery_api_port == 9291
    assert cfg.battery_metrics_port == 9292
    assert cfg.battery_sweep_interval == "1s"
    assert cfg.battery_warning_window == "1s"
    assert cfg.battery_log_level == "warn"
    assert cfg.timeout_pool_available == 600


def test_backend_defaults_to_digitalocean(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.delenv("INFRA_BACKEND", raising=False)
    cfg = load(dotenv_path=empty_env)
    assert cfg.infra_backend == "digitalocean"
    assert cfg.libvirt_uri == "qemu:///system"
    assert cfg.libvirt_pool == "lm-acceptance"
    assert cfg.libvirt_subnet_prefix == "10.210"
    assert (cfg.libvirt_vcpus, cfg.libvirt_memory_mb, cfg.libvirt_disk_gb) == (4, 8192, 50)
    assert "release-20260926" in cfg.libvirt_base_image_url
    assert len(cfg.libvirt_base_image_sha256) == 64


def test_digitalocean_backend_still_requires_token(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.delenv("DO_API_TOKEN")
    with pytest.raises(ConfigError, match="DO_API_TOKEN"):
        load(dotenv_path=empty_env)


def test_libvirt_backend_needs_no_do_token(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.delenv("DO_API_TOKEN")
    monkeypatch.setenv("INFRA_BACKEND", "libvirt")
    monkeypatch.setenv("LIBVIRT_VCPUS", "2")
    cfg = load(dotenv_path=empty_env)
    assert cfg.infra_backend == "libvirt"
    assert cfg.do_token == ""
    assert cfg.libvirt_vcpus == 2


def test_invalid_backend_raises(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("INFRA_BACKEND", "qemu")
    with pytest.raises(ConfigError, match="INFRA_BACKEND"):
        load(dotenv_path=empty_env)


def test_libvirt_run_id_must_not_shadow_base_images(monkeypatch, tmp_path):
    # Base volumes are named lm-acceptance-base-*; a run id starting with "base" would
    # make that run's volumes look like base images and they would never be cleaned up.
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("INFRA_BACKEND", "libvirt")
    monkeypatch.setenv("RUN_ID", "base-1")
    with pytest.raises(ConfigError, match="RUN_ID"):
        load(dotenv_path=empty_env)


def test_libvirt_subnet_prefix_must_be_two_octets(monkeypatch, tmp_path):
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("INFRA_BACKEND", "libvirt")
    monkeypatch.setenv("LIBVIRT_SUBNET_PREFIX", "10.210.0.0/16")
    with pytest.raises(ConfigError, match="LIBVIRT_SUBNET_PREFIX"):
        load(dotenv_path=empty_env)


@pytest.mark.parametrize("run_id", ["has space", "a/b", "quote'd", "-leading-dash", "under_score"])
def test_libvirt_run_id_must_be_a_safe_name(monkeypatch, tmp_path, run_id):
    # The run id becomes libvirt domain/network/volume names, XML attributes and a hostname.
    empty_env = _prime_required(monkeypatch, tmp_path)
    monkeypatch.setenv("INFRA_BACKEND", "libvirt")
    monkeypatch.setenv("RUN_ID", run_id)
    with pytest.raises(ConfigError, match="RUN_ID"):
        load(dotenv_path=empty_env)
