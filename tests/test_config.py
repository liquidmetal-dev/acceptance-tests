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
