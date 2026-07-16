"""Offline checks for config parsing.

No DigitalOcean token or infra needed. Guards the ``.env`` inline-comment footgun:
python-dotenv leaves an inline comment on an empty-value line as the value, which
must not leak into DigitalOcean resource names (which allow only ``[A-Za-z0-9.-]``).
"""
from __future__ import annotations

import re

from liquidmetal_at.config import _env, load

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
