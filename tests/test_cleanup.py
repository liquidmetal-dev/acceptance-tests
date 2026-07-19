"""Offline checks for teardown idempotency + spec/template builders.

These need neither DigitalOcean nor a cluster, so they run in CI without secrets and
guard the logic that the e2e path depends on. The teardown check uses a fake DO client
that mutates its own state on delete, proving ``destroy_by_tag`` is safe to call twice.
"""
from __future__ import annotations

import types

import pytest

from liquidmetal_at import brigade_status
from liquidmetal_at.bootstrap import host as host_mod
from liquidmetal_at.bootstrap.render import render
from liquidmetal_at.config import Config, ConfigError, _validate_microvm_shape
from liquidmetal_at.infra import do


def _dummy_config(tmp_path) -> Config:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAATESTKEY test@runner")
    priv.write_text("PRIVATE")
    return Config(
        do_token="x",
        do_region="nyc3",
        do_size="s-4vcpu-8gb",
        do_image="ubuntu-22-04-x64",
        do_volume_gb=50,
        run_id="at-deadbeef",
        ssh_public_key_path=str(pub),
        ssh_private_key_path=str(priv),
        microvm_kernel_image="ghcr.io/example/kernel:5.10",
        microvm_rootfs_image="ghcr.io/example/rootfs:1.0",
        microvm_kernel_filename="boot/vmlinux",
        microvm_namespace="at-deadbeef",
        microvm_vcpu=1,
        microvm_mem_mb=1024,
        microvm_count=4,
        microvm_subnet_cidr="192.168.100.0/24",
        brigade_grpc_port=9091,
        brigade_status_port=9600,
        brigade_cookie="at-deadbeef",
        brigade_min_cluster_size=2,
        brigade_ref="main",
        flintlock_ref="main",
        flintlock_grpc_port=9090,
        timeout_provision=300,
        timeout_bootstrap=1200,
        timeout_cluster=180,
        timeout_vm_create=300,
        timeout_vm_delete=120,
        timeout_ssh=180,
        keep_infra_on_failure=False,
        artifacts_dir=str(tmp_path / "artifacts"),
    )


class _FakeNS:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def list(self):
        return {self.key: list(self.store[self.key])}


class _FakeDO:
    """Minimal pydo stand-in whose state shrinks as resources are deleted."""

    def __init__(self, tag):
        self.tag = tag
        self._fw = [{"name": tag, "id": "fw1"}]
        self._droplets = [{"id": 1, "tags": [tag]}]
        self._vols = [{"id": "vol1", "name": f"{tag}-host0-pool", "tags": [tag]}]
        self._vpcs = [{"name": tag, "id": "vpc1"}]
        self.deletes: list[str] = []
        self.firewalls = self._make("firewalls", self._fw)
        self.droplets = self._make_droplets()
        self.volumes = self._make("volumes", self._vols)
        self.vpcs = self._make("vpcs", self._vpcs)

    def _make(self, key, backing):
        outer = self

        class NS:
            def list(self):
                return {key: list(backing)}

            def delete(self, **kw):
                _id = next(iter(kw.values()))
                outer.deletes.append(f"{key}:{_id}")
                backing[:] = [x for x in backing if x.get("id") != _id]

        return NS()

    def _make_droplets(self):
        outer = self

        class NS:
            def destroy_by_tag(self, tag_name):
                outer.deletes.append(f"droplets:{tag_name}")
                outer._droplets[:] = []

        return NS()


def test_destroy_by_tag_is_idempotent(tmp_path):
    cfg = _dummy_config(tmp_path)
    fake = _FakeDO(cfg.tag)

    do.destroy_by_tag(cfg, fake)
    first = list(fake.deletes)
    assert any(d.startswith("firewalls:") for d in first)
    assert any(d.startswith("droplets:") for d in first)
    assert any(d.startswith("volumes:") for d in first)
    assert any(d.startswith("vpcs:") for d in first)

    # Second call: everything already gone → no new deletions, no error.
    before = len(fake.deletes)
    do.destroy_by_tag(cfg, fake)
    new = fake.deletes[before:]
    # droplets.destroy_by_tag is always issued (DO no-ops on empty tag); the rest must not re-fire.
    assert all(d.startswith("droplets:") for d in new)


def test_destroy_by_tag_tolerates_missing_tag(tmp_path):
    """Provisioning that died before the first droplet leaves no tag; DO's
    destroy_by_tag then 404s "tag ... does not exist". Teardown must swallow that
    and still reap volumes + VPC, not abort."""
    cfg = _dummy_config(tmp_path)
    fake = _FakeDO(cfg.tag)

    def _boom(tag_name):
        raise RuntimeError(f"tag {tag_name} does not exist")

    fake.droplets.destroy_by_tag = _boom

    do.destroy_by_tag(cfg, fake)  # must not raise
    assert any(d.startswith("volumes:") for d in fake.deletes)
    assert any(d.startswith("vpcs:") for d in fake.deletes)


def test_static_ip_allocation(tmp_path):
    cfg = _dummy_config(tmp_path)
    assert cfg.microvm_static_ip(0) == "192.168.100.10/24"
    assert cfg.microvm_static_ip(3) == "192.168.100.13/24"


def test_capacity_forces_spread(tmp_path):
    cfg = _dummy_config(tmp_path)
    vcpu, mem = host_mod._capacity(cfg)
    # A single host must not hold all N VMs.
    assert vcpu < 1 + cfg.microvm_count * cfg.microvm_vcpu
    assert (vcpu - 1) // cfg.microvm_vcpu == cfg.microvm_count - 1


def test_renders_are_valid():
    ci = render("cloud_init.yaml.j2", flintlock_ref="main", brigade_ref="main")
    assert ci.startswith("#cloud-config")
    exs = render(
        "brigade_config.exs.j2",
        grpc_port=9091,
        status_port=9600,
        flintlock_grpc_port=9090,
        private_ip="10.0.0.2",
        min_cluster_size=2,
        node_index=0,
        capacity_vcpu=4,
        capacity_mem_mb=8192,
        erlang_hosts=':"brigade@10.0.0.2", :"brigade@10.0.0.3"',
    )
    assert "min_cluster_size: 2" in exs
    assert "Cluster.Strategy.Epmd" in exs
    # scheduler_strategy has no code default in brigade; absence crashes CreateMicroVM.
    assert "scheduler_strategy: Brigade.Scheduler.Strategy.LeastLoaded" in exs
    # The host endpoint must be the node's private IP (not localhost), or the scheduler
    # dials its own flintlock for every placement and VMs never spread across hosts.
    assert 'flintlock_endpoint: "10.0.0.2:9090"' in exs

    # provision_host must create a bridge and hand it to flintlockd, or TAP creation fails.
    host_sh = render(
        "provision_host.sh.j2",
        thinpool="tp",
        disk="/dev/sda",
        parent_iface="eth1",
        bridge_name="flintlock0",
        bridge_addr="192.168.100.1/24",
        guest_subnet="192.168.100.0/24",
        flintlock_grpc_port=9090,
    )
    assert "ip link add name" in host_sh and "type bridge" in host_sh
    assert "--bridge-name=${BRIDGE}" in host_sh


def test_cluster_size_reads_partition(monkeypatch):
    # Real brigade /status (trimmed): membership lives under `partition`, while top-level
    # `hosts` holds only this node's own managed host. cluster_size must return 2, not 1.
    payload = {
        "node": "brigade@10.106.32.2",
        "hosts": [{"id": "brigade@10.106.32.2", "vm_count": 0}],
        "partition": {
            "size": 2,
            "min_cluster_size": 2,
            "members": ["brigade@10.106.32.2", "brigade@10.106.32.3"],
            "in_quorum": True,
        },
    }
    monkeypatch.setattr(brigade_status, "_get", lambda *a, **k: payload)
    assert brigade_status.cluster_size("x", 9600) == 2


def test_validate_microvm_shape_rejects_flintlock_invalids():
    # Valid baseline passes.
    _validate_microvm_shape(1024, 1, "boot/vmlinux")
    # Below flintlock's gte=1024 memory minimum.
    with pytest.raises(ConfigError):
        _validate_microvm_shape(512, 1, "boot/vmlinux")
    # Empty Kernel.Filename (required tag).
    with pytest.raises(ConfigError):
        _validate_microvm_shape(1024, 1, "")
    # vCPU out of range.
    with pytest.raises(ConfigError):
        _validate_microvm_shape(1024, 0, "boot/vmlinux")


def test_create_retries_only_transient_codes():
    import grpc

    from liquidmetal_at.flintlock import client as fc

    class _Err(grpc.RpcError):
        def __init__(self, code):
            self._code = code

        def code(self):
            return self._code

        def details(self):
            return "boom"

    class _Stub:
        def __init__(self, code):
            self.code = code
            self.calls = 0

        def CreateMicroVM(self, req, timeout=None):
            self.calls += 1
            if self.code is not None:
                raise _Err(self.code)
            return types.SimpleNamespace(microvm="VM")

    c = fc.FlintlockClient("127.0.0.1", 9091)
    try:
        # Non-transient (bad spec) surfaces immediately, unwrapped, no retry.
        c._stub = _Stub(grpc.StatusCode.INVALID_ARGUMENT)
        with pytest.raises(grpc.RpcError):
            c.create(object(), retry_timeout=0)

        # Transient codes are wrapped as retryable; with retry_timeout=0 it gives up fast.
        c._stub = _Stub(grpc.StatusCode.RESOURCE_EXHAUSTED)
        with pytest.raises(fc._TransientSchedulerError):
            c.create(object(), retry_timeout=0)

        # Happy path returns the microvm.
        c._stub = _Stub(None)
        assert c.create(object(), retry_timeout=0) == "VM"
    finally:
        c.close()


def test_build_spec(tmp_path):
    cfg = _dummy_config(tmp_path)
    from liquidmetal_at.flintlock import spec

    req = spec.build_create_request(cfg, 2)
    s = req.microvm
    assert s.id == "at-deadbeef-vm2"
    assert s.vcpu == 1
    assert s.memory_in_mb >= 1024
    assert s.kernel.image == cfg.microvm_kernel_image
    # flintlock rejects an empty Kernel.Filename (required tag).
    assert s.kernel.filename == "boot/vmlinux"
    assert s.root_volume.source.container_source == cfg.microvm_rootfs_image
    assert s.interfaces[0].address.address == "192.168.100.12/24"
    # TAP guests need a gateway (the host bridge); flintlock validates it as CIDR.
    assert s.interfaces[0].address.gateway == "192.168.100.1/24"
    # A per-VM guest MAC makes flintlock's netplan match by MAC (not by name), so the guest
    # NIC binds regardless of eth1/ens4 naming. Deterministic + unique per index.
    assert s.interfaces[0].guest_mac == "aa:ff:00:00:00:02"
    assert "user-data" in s.metadata and "meta-data" in s.metadata
    # Provider defaults to firecracker.
    assert s.provider == "firecracker"
    # Firecracker carries its own ds= hint via MMDS/cmdline in flintlock; we must not inject one.
    assert "ds" not in dict(s.kernel.cmdline)


def test_build_spec_cloudhypervisor(tmp_path):
    from dataclasses import replace

    from liquidmetal_at.flintlock import spec

    cfg = replace(
        _dummy_config(tmp_path),
        microvm_provider="cloudhypervisor",
        microvm_ch_kernel_image="ghcr.io/example/ch-kernel:6.1",
        microvm_ch_kernel_filename="boot/compressed-vmlinux.bin",
    )
    s = spec.build_create_request(cfg, 0).microvm
    # provider flows into the spec; CH kernel overrides win when set.
    assert s.provider == "cloudhypervisor"
    assert s.kernel.image == "ghcr.io/example/ch-kernel:6.1"
    assert s.kernel.filename == "boot/compressed-vmlinux.bin"
    # CH has no MMDS/cmdline datasource hint, so we force NoCloud to make the guest's
    # cloud-init read the cidata disk (network-config) — without it the NIC never comes up.
    assert dict(s.kernel.cmdline).get("ds") == "nocloud"
    # guest_mac drives netplan match-by-MAC so CH's ens4-named NIC still binds; index 0 here.
    assert s.interfaces[0].guest_mac == "aa:ff:00:00:00:00"
