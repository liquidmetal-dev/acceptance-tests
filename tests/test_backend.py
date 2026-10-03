"""Offline checks for backend dispatch + the shared provision/teardown fixture body."""
from __future__ import annotations

import dataclasses

import pytest
from test_cleanup import _dummy_config

from liquidmetal_at.bootstrap import cloudinit
from liquidmetal_at.infra import backend
from liquidmetal_at.infra.types import Infra, Node


@pytest.fixture
def calls(monkeypatch):
    """Replace both provisioners + log collection with recorders."""
    seen: list[str] = []

    def fake_provision(name):
        def _p(cfg, user_data_for):
            seen.append(f"{name}.provision")
            return Infra(cfg=cfg, nodes=[Node("n0", "1.1.1.1", "1.1.1.1")])

        return _p

    monkeypatch.setattr(backend.do, "provision", fake_provision("do"))
    monkeypatch.setattr(backend.libvirt, "provision", fake_provision("libvirt"))
    monkeypatch.setattr(
        backend.do, "destroy_by_tag", lambda cfg, client=None: seen.append("do.destroy")
    )
    monkeypatch.setattr(backend.libvirt, "destroy_run", lambda cfg: seen.append("libvirt.destroy"))
    monkeypatch.setattr(
        backend.libvirt, "collect_consoles", lambda cfg: seen.append("libvirt.consoles")
    )
    monkeypatch.setattr(backend.logs, "collect", lambda cfg, infra: seen.append("logs.collect"))
    return seen


def _cfg(tmp_path, backend_name, **kw):
    return dataclasses.replace(_dummy_config(tmp_path), infra_backend=backend_name, **kw)


def _run(cfg, failed):
    with backend.provisioned_infra(cfg, lambda i, name: "", failed=lambda: failed) as infra:
        assert infra.nodes[0].public_ip == "1.1.1.1"


def test_digitalocean_success_path(tmp_path, calls):
    _run(_cfg(tmp_path, "digitalocean"), failed=False)
    assert calls == ["do.provision", "do.destroy"]


def test_libvirt_success_path(tmp_path, calls):
    _run(_cfg(tmp_path, "libvirt"), failed=False)
    assert calls == ["libvirt.provision", "libvirt.destroy"]


def test_failure_collects_logs_before_teardown(tmp_path, calls):
    _run(_cfg(tmp_path, "libvirt"), failed=True)
    assert calls == ["libvirt.provision", "logs.collect", "libvirt.consoles", "libvirt.destroy"]


def test_do_failure_does_not_touch_libvirt(tmp_path, calls):
    _run(_cfg(tmp_path, "digitalocean"), failed=True)
    assert calls == ["do.provision", "logs.collect", "do.destroy"]


def test_keep_infra_on_failure_skips_teardown(tmp_path, calls):
    _run(_cfg(tmp_path, "libvirt", keep_infra_on_failure=True), failed=True)
    assert "libvirt.destroy" not in calls


def test_keep_infra_only_applies_to_failures(tmp_path, calls):
    _run(_cfg(tmp_path, "libvirt", keep_infra_on_failure=True), failed=False)
    assert calls[-1] == "libvirt.destroy"


def test_log_collection_errors_never_leak_infra(tmp_path, calls, monkeypatch):
    def boom(cfg, infra):
        raise RuntimeError("ssh down")

    monkeypatch.setattr(backend.logs, "collect", boom)
    _run(_cfg(tmp_path, "libvirt"), failed=True)
    assert calls[-1] == "libvirt.destroy"


def test_cloud_init_enables_root_ssh_only_on_libvirt(tmp_path):
    do_data = cloudinit.user_data(_cfg(tmp_path, "digitalocean"), 0, "n0")
    lv_data = cloudinit.user_data(_cfg(tmp_path, "libvirt"), 0, "n0")
    assert "disable_root" not in do_data
    assert "disable_root: false" in lv_data
    assert "      - ssh-ed25519 AAAATESTKEY test@runner" in lv_data
    assert lv_data.startswith("#cloud-config")
