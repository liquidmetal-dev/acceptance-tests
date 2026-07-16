"""Offline checks for teardown idempotency + spec/template builders.

These need neither DigitalOcean nor a cluster, so they run in CI without secrets and
guard the logic that the e2e path depends on. The teardown check uses a fake DO client
that mutates its own state on delete, proving ``destroy_by_tag`` is safe to call twice.
"""
from __future__ import annotations

from liquidmetal_at.bootstrap import host as host_mod
from liquidmetal_at.bootstrap.render import render
from liquidmetal_at.config import Config
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
        microvm_kernel_filename="",
        microvm_namespace="at-deadbeef",
        microvm_vcpu=1,
        microvm_mem_mb=512,
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
        min_cluster_size=2,
        node_index=0,
        capacity_vcpu=4,
        capacity_mem_mb=8192,
        erlang_hosts=':"brigade@10.0.0.2", :"brigade@10.0.0.3"',
    )
    assert "min_cluster_size: 2" in exs
    assert "Cluster.Strategy.Epmd" in exs


def test_build_spec(tmp_path):
    cfg = _dummy_config(tmp_path)
    from liquidmetal_at.flintlock import spec

    req = spec.build_create_request(cfg, 2)
    s = req.microvm
    assert s.id == "at-deadbeef-vm2"
    assert s.vcpu == 1
    assert s.kernel.image == cfg.microvm_kernel_image
    assert s.root_volume.source.container_source == cfg.microvm_rootfs_image
    assert s.interfaces[0].address.address == "192.168.100.12/24"
    assert "user-data" in s.metadata and "meta-data" in s.metadata
