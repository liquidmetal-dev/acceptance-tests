"""Offline checks for the libvirt backend.

No libvirt needed: every virsh/virt-install call goes through an injectable runner, and
``FakeVirsh`` is a stateful stand-in whose pools, volumes, networks and domains change as
commands run — so idempotency and ordering are provable without a hypervisor.
"""
from __future__ import annotations

import dataclasses
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_cleanup import _dummy_config

from liquidmetal_at.infra import libvirt
from liquidmetal_at.infra.libvirt import LibvirtError

URI = "qemu:///system"


def _cfg(tmp_path, **overrides):
    base = dataclasses.replace(_dummy_config(tmp_path), infra_backend="libvirt", run_id="at-1")
    return dataclasses.replace(base, **overrides)


class FakeVirsh:
    """Stateful stand-in for virsh + virt-install."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.pools: dict[str, bool] = {}  # name -> active
        self.vols: list[str] = []
        self.nets: dict[str, str] = {}  # name -> gateway address
        self.net_xml: dict[str, str] = {}
        self.macs: list[str] = []
        self.domains: list[str] = []
        self.leases: str | None = None  # None -> every defined MAC has a lease
        self.busy_gateways: set[str] = set()
        self.fail: dict[str, str] = {}  # command -> error text
        self.fail_install_of: str | None = None

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if argv[0] == "virt-install":
            name = argv[argv.index("--name") + 1]
            if name == self.fail_install_of:
                raise LibvirtError(f"virt-install failed for {name}")
            self.domains.append(name)
            return ""
        assert argv[:3] == ["virsh", "-c", URI], argv
        cmd, args = argv[3], argv[4:]
        if cmd in self.fail:
            raise LibvirtError(self.fail[cmd])
        return getattr(self, "_" + cmd.replace("-", "_"))(args) or ""

    def ran(self, cmd: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == cmd or (len(c) > 3 and c[3] == cmd)]

    # --- hypervisor / pools ---
    def _version(self, args):
        return "Running hypervisor: QEMU 8.2.0\n"

    def _pool_list(self, args):
        names = [n for n, active in self.pools.items() if active or "--all" in args]
        return "\n".join(names) + "\n"

    def _pool_define_as(self, args):
        self.pools[args[0]] = False

    def _pool_build(self, args):
        pass

    def _pool_start(self, args):
        self.pools[args[0]] = True

    def _pool_refresh(self, args):
        pass

    def _pool_dumpxml(self, args):
        return f"<pool><target><path>/pool/{args[0]}</path></target></pool>"

    # --- volumes ---
    def _vol_list(self, args):
        rows = "".join(f" {v}   /pool/{args[0]}/{v}\n" for v in self.vols)
        return " Name   Path\n" + "-" * 20 + "\n" + rows + "\n"

    def _vol_create_as(self, args):
        self.vols.append(args[1])

    def _vol_upload(self, args):
        pass

    def _vol_delete(self, args):
        self.vols.remove(args[-1])

    def _vol_download(self, args):
        Path(args[-1]).write_text(f"console of {args[-2]}\n")

    # --- networks ---
    def _net_list(self, args):
        return "\n".join(self.nets) + "\n"

    def _net_dumpxml(self, args):
        gw = self.nets[args[0]]
        ip = f"<ip address='{gw}' netmask='255.255.255.0'/>"
        return f"<network><name>{args[0]}</name>{ip}</network>"

    def _net_define(self, args):
        text = Path(args[0]).read_text()
        root = ET.fromstring(text)
        name = root.findtext("name")
        self.nets[name] = root.find("ip").get("address")
        self.net_xml[name] = text
        self.macs += [h.get("mac") for h in root.iter("host")]

    def _net_start(self, args):
        if self.nets[args[0]] in self.busy_gateways:
            raise LibvirtError("error: Address already in use")

    def _net_destroy(self, args):
        pass

    def _net_undefine(self, args):
        del self.nets[args[0]]

    def _net_dhcp_leases(self, args):
        return " ".join(self.macs) if self.leases is None else self.leases

    # --- domains ---
    def _list(self, args):
        return "\n".join(self.domains) + "\n"

    def _destroy(self, args):
        pass

    def _undefine(self, args):
        self.domains.remove(args[0])


@pytest.fixture
def fake():
    return FakeVirsh()


@pytest.fixture
def host_ok(monkeypatch, tmp_path):
    """A host that passes preflight: tools on PATH, /dev/kvm, nested virt enabled."""
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    nested = tmp_path / "nested"
    nested.write_text("1\n")
    monkeypatch.setattr(libvirt.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(libvirt, "KVM_DEVICE", kvm)
    monkeypatch.setattr(libvirt, "NESTED_PARAMS", (tmp_path / "absent", nested))
    return tmp_path


# --- preflight ---------------------------------------------------------------


def test_preflight_passes_on_a_ready_host(tmp_path, fake, host_ok):
    libvirt.preflight(_cfg(tmp_path), fake)
    assert fake.ran("version")


def test_preflight_names_the_missing_tool(tmp_path, fake, host_ok, monkeypatch):
    monkeypatch.setattr(
        libvirt.shutil, "which", lambda tool: None if tool == "virt-install" else "/usr/bin/x"
    )
    with pytest.raises(LibvirtError, match="virt-install"):
        libvirt.preflight(_cfg(tmp_path), fake)


def test_preflight_requires_kvm_device(tmp_path, fake, host_ok, monkeypatch):
    monkeypatch.setattr(libvirt, "KVM_DEVICE", tmp_path / "no-kvm")
    with pytest.raises(LibvirtError, match="KVM is required"):
        libvirt.preflight(_cfg(tmp_path), fake)


def test_preflight_requires_nested_virt(tmp_path, fake, host_ok, monkeypatch):
    off = tmp_path / "off"
    off.write_text("0\n")
    monkeypatch.setattr(libvirt, "NESTED_PARAMS", (off,))
    with pytest.raises(LibvirtError, match="nested"):
        libvirt.preflight(_cfg(tmp_path), fake)


def test_preflight_explains_unreachable_daemon(tmp_path, fake, host_ok):
    fake.fail["version"] = "failed to connect to the hypervisor"
    with pytest.raises(LibvirtError, match="libvirt group"):
        libvirt.preflight(_cfg(tmp_path), fake)


# --- pool --------------------------------------------------------------------


def test_ensure_pool_creates_and_starts_missing_pool(tmp_path, fake):
    libvirt.ensure_pool(_cfg(tmp_path), fake)
    assert fake.pools == {"lm-acceptance": True}
    define = fake.ran("pool-define-as")[0]
    target = "/var/lib/libvirt/images/lm-acceptance"
    assert define[4:] == ["lm-acceptance", "dir", "--target", target]


def test_ensure_pool_is_a_noop_when_active(tmp_path, fake):
    fake.pools["lm-acceptance"] = True
    libvirt.ensure_pool(_cfg(tmp_path), fake)
    assert not fake.ran("pool-define-as") and not fake.ran("pool-start")


def test_ensure_pool_starts_an_inactive_pool(tmp_path, fake):
    fake.pools["lm-acceptance"] = False
    libvirt.ensure_pool(_cfg(tmp_path), fake)
    assert fake.pools["lm-acceptance"] is True
    assert not fake.ran("pool-define-as")


def test_pool_path_reads_target(tmp_path, fake):
    assert libvirt.pool_path(_cfg(tmp_path), fake) == "/pool/lm-acceptance"


# --- base image --------------------------------------------------------------


def _image(tmp_path, monkeypatch, content=b"cloud image bytes"):
    """A local 'remote' image + isolated cache dir; returns (url, sha256)."""
    src = tmp_path / "src" / "release-20260926" / "ubuntu.img"
    src.parent.mkdir(parents=True)
    src.write_bytes(content)
    monkeypatch.setattr(libvirt, "CACHE_DIR", tmp_path / "cache")
    return src.as_uri(), hashlib.sha256(content).hexdigest()


def test_base_volume_name_carries_release_date_and_checksum(tmp_path):
    cfg = _cfg(
        tmp_path,
        libvirt_base_image_url="https://example.test/release-20260926/x.img",
        libvirt_base_image_sha256="ab" * 32,
    )
    assert libvirt.base_volume_name(cfg) == "lm-acceptance-base-20260926-abababab.qcow2"


def test_base_volume_name_without_a_dated_url(tmp_path):
    cfg = _cfg(
        tmp_path,
        libvirt_base_image_url="https://example.test/current/x.img",
        libvirt_base_image_sha256="cd" * 32,
    )
    assert libvirt.base_volume_name(cfg) == "lm-acceptance-base-undated-cdcdcdcd.qcow2"


def test_ensure_base_image_downloads_verifies_and_uploads_once(tmp_path, fake, monkeypatch):
    url, sha = _image(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path, libvirt_base_image_url=url, libvirt_base_image_sha256=sha)

    name = libvirt.ensure_base_image(cfg, fake)
    assert name == libvirt.base_volume_name(cfg)
    assert fake.vols == [name, f"{name}.ok"]
    assert len(fake.ran("vol-upload")) == 1

    libvirt.ensure_base_image(cfg, fake)  # second run reuses the volume
    assert len(fake.ran("vol-upload")) == 1


def test_checksum_mismatch_raises_and_caches_nothing(tmp_path, fake, monkeypatch):
    url, _ = _image(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path, libvirt_base_image_url=url, libvirt_base_image_sha256="0" * 64)
    with pytest.raises(LibvirtError, match="checksum"):
        libvirt.ensure_base_image(cfg, fake)
    assert [p.name for p in (tmp_path / "cache").iterdir()] == ["base-image.lock"]
    assert fake.vols == []


def test_failed_upload_removes_partial_base_volume(tmp_path, fake, monkeypatch):
    url, sha = _image(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path, libvirt_base_image_url=url, libvirt_base_image_sha256=sha)
    fake.fail["vol-upload"] = "connection reset"
    with pytest.raises(LibvirtError, match="connection reset"):
        libvirt.ensure_base_image(cfg, fake)
    assert fake.vols == []


def test_interrupted_upload_is_not_trusted(tmp_path, fake, monkeypatch):
    # A killed run (Ctrl-C, cancelled CI job) can leave a truncated base volume behind.
    # Without its completion marker it must be replaced, not reused by every later run.
    url, sha = _image(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path, libvirt_base_image_url=url, libvirt_base_image_sha256=sha)
    name = libvirt.base_volume_name(cfg)
    fake.vols = [name]

    libvirt.ensure_base_image(cfg, fake)

    assert fake.ran("vol-delete")[0][-1] == name
    assert len(fake.ran("vol-upload")) == 1
    assert fake.vols == [name, f"{name}.ok"]


def test_sweep_keeps_base_image_completion_marker(fake):
    fake.pools["lm-acceptance"] = True
    fake.vols = [BASE, f"{BASE}.ok"]
    libvirt.sweep(URI, "lm-acceptance", fake)
    assert fake.vols == [BASE, f"{BASE}.ok"]


# --- teardown / sweep / consoles ---------------------------------------------

BASE = "lm-acceptance-base-20260926-0c9811a8.qcow2"


def _seed_two_runs(fake):
    """Runs at-1 and at-10 side by side (one id is a prefix of the other) + a base image."""
    fake.pools["lm-acceptance"] = True
    fake.vols = [BASE]
    for run_id in ("at-1", "at-10"):
        tag = f"lm-acceptance-{run_id}"
        fake.nets[tag] = "10.210.0.1"
        for i in (0, 1):
            fake.domains.append(f"{tag}-host{i}")
            fake.vols += [f"{tag}-host{i}.qcow2", f"{tag}-host{i}-console.log"]


def test_destroy_run_leaves_other_runs_and_base_image(tmp_path, fake):
    _seed_two_runs(fake)
    libvirt.destroy_run(_cfg(tmp_path, run_id="at-1"), fake)

    assert fake.domains == ["lm-acceptance-at-10-host0", "lm-acceptance-at-10-host1"]
    assert list(fake.nets) == ["lm-acceptance-at-10"]
    assert BASE in fake.vols
    assert not [v for v in fake.vols if v.startswith("lm-acceptance-at-1-")]
    assert len([v for v in fake.vols if v.startswith("lm-acceptance-at-10-")]) == 4


def test_destroy_run_is_idempotent(tmp_path, fake):
    _seed_two_runs(fake)
    cfg = _cfg(tmp_path, run_id="at-1")
    libvirt.destroy_run(cfg, fake)
    before = len(fake.calls)
    libvirt.destroy_run(cfg, fake)  # nothing left: must not raise or delete anything
    new = fake.calls[before:]
    assert not [c for c in new if c[3] in ("undefine", "vol-delete", "net-undefine")]


def test_destroy_run_tolerates_already_stopped_domains_and_networks(tmp_path, fake):
    _seed_two_runs(fake)
    fake.fail["destroy"] = "domain is not running"
    fake.fail["net-destroy"] = "network is not active"
    libvirt.destroy_run(_cfg(tmp_path, run_id="at-1"), fake)
    assert "lm-acceptance-at-1-host0" not in fake.domains
    assert "lm-acceptance-at-1" not in fake.nets


def test_destroy_run_without_pool_still_removes_domains(tmp_path, fake):
    fake.domains = ["lm-acceptance-at-1-host0"]
    libvirt.destroy_run(_cfg(tmp_path, run_id="at-1"), fake)
    assert fake.domains == []
    assert not fake.ran("vol-list")


def test_sweep_removes_every_run_but_keeps_base_images(fake):
    _seed_two_runs(fake)
    fake.domains.append("someone-elses-vm")
    fake.nets["default"] = "192.168.122.1"
    libvirt.sweep(URI, "lm-acceptance", fake)
    assert fake.domains == ["someone-elses-vm"]
    assert list(fake.nets) == ["default"]
    assert fake.vols == [BASE]


def test_collect_consoles_saves_this_runs_logs(tmp_path, fake):
    _seed_two_runs(fake)
    cfg = _cfg(tmp_path, run_id="at-1")
    libvirt.collect_consoles(cfg, fake)
    out = Path(cfg.artifacts_dir) / "at-1"
    assert sorted(p.name for p in out.iterdir()) == ["host0-console.log", "host1-console.log"]
    assert "lm-acceptance-at-1-host0-console.log" in (out / "host0-console.log").read_text()


def test_collect_consoles_never_raises(tmp_path, fake):
    fake.fail["pool-list"] = "daemon gone"
    libvirt.collect_consoles(_cfg(tmp_path), fake)  # best effort


# --- network / VMs / provision -----------------------------------------------


def _user_data(index, name):
    return f"#cloud-config\n# {name}\n"


@pytest.fixture
def ready(monkeypatch, tmp_path, fake, host_ok):
    """A host where provision() can run end to end against the fake."""
    url, sha = _image(tmp_path, monkeypatch)
    monkeypatch.setattr(libvirt, "_wait_ssh", lambda cfg, ip: None)
    return _cfg(tmp_path, libvirt_base_image_url=url, libvirt_base_image_sha256=sha)


def test_network_xml_pins_each_node_to_a_fixed_address(tmp_path):
    xml = libvirt.network_xml(_cfg(tmp_path), "10.210.7")
    root = ET.fromstring(xml)
    assert root.findtext("name") == "lm-acceptance-at-1"
    assert root.find("forward").get("mode") == "nat"
    assert root.find("ip").get("address") == "10.210.7.1"
    hosts = [(h.get("name"), h.get("ip"), h.get("mac")) for h in root.iter("host")]
    assert hosts == [
        ("lm-acceptance-at-1-host0", "10.210.7.10", "52:54:00:d2:07:0a"),
        ("lm-acceptance-at-1-host1", "10.210.7.11", "52:54:00:d2:07:0b"),
    ]


def test_pick_subnet_skips_used(tmp_path, fake):
    fake.nets = {"default": "192.168.122.1", "a": "10.210.0.1", "b": "10.210.1.1"}
    assert libvirt.pick_subnet(_cfg(tmp_path), fake) == "10.210.2"


def test_create_network_moves_on_when_start_fails(tmp_path, fake):
    # 10.210.0.1 is taken by something libvirt doesn't know about (or a concurrent run).
    fake.busy_gateways = {"10.210.0.1"}
    subnet = libvirt.create_network(_cfg(tmp_path), fake)
    assert subnet == "10.210.1"
    assert fake.nets == {"lm-acceptance-at-1": "10.210.1.1"}


def test_provision_returns_nodes_on_fixed_addresses(ready, fake):
    infra = libvirt.provision(ready, _user_data, fake)
    assert [(n.name, n.public_ip, n.private_ip, n.parent_iface) for n in infra.nodes] == [
        ("lm-acceptance-at-1-host0", "10.210.0.10", "10.210.0.10", None),
        ("lm-acceptance-at-1-host1", "10.210.0.11", "10.210.0.11", None),
    ]
    assert fake.domains == ["lm-acceptance-at-1-host0", "lm-acceptance-at-1-host1"]
    assert infra.cfg is ready


def test_virt_install_arguments(ready, fake):
    libvirt.provision(ready, _user_data, fake)
    argv = fake.ran("virt-install")[0]

    def opt(flag):
        return argv[argv.index(flag) + 1]

    assert opt("--connect") == URI
    assert opt("--memory") == "8192" and opt("--vcpus") == "4"
    assert opt("--cpu") == "host-passthrough"
    assert opt("--osinfo") == "ubuntu22.04"
    assert opt("--disk") == "vol=lm-acceptance/lm-acceptance-at-1-host0.qcow2,bus=virtio"
    assert opt("--network") == "network=lm-acceptance-at-1,mac=52:54:00:d2:00:0a,model=virtio"
    assert opt("--serial") == "file,path=/pool/lm-acceptance/lm-acceptance-at-1-host0-console.log"
    assert opt("--cloud-init").startswith("user-data=")
    assert "--import" in argv and "--noautoconsole" in argv


def test_overlay_is_backed_by_the_base_image(ready, fake):
    libvirt.provision(ready, _user_data, fake)
    create = [c for c in fake.ran("vol-create-as") if c[5].endswith("host0.qcow2")][0]
    assert create[6] == "50G"
    assert create[create.index("--backing-vol") + 1] == libvirt.base_volume_name(ready)


def test_no_dhcp_lease_fails_fast_with_firewall_hint(ready, fake, monkeypatch):
    fake.leases = ""  # the bridge's DHCP never answers
    monkeypatch.setattr(libvirt, "LEASE_TIMEOUT", 0)
    with pytest.raises(LibvirtError, match="firewall_backend"):
        libvirt.provision(ready, _user_data, fake)


def test_failed_provision_tears_down_and_keeps_console(ready, fake):
    fake.fail_install_of = "lm-acceptance-at-1-host1"
    fake.vols.append("lm-acceptance-at-1-host0-console.log")  # QEMU wrote host0's console
    with pytest.raises(LibvirtError, match="host1"):
        libvirt.provision(ready, _user_data, fake)

    assert fake.domains == []
    assert fake.nets == {}
    base = libvirt.base_volume_name(ready)
    assert fake.vols == [base, f"{base}.ok"]
    assert (Path(ready.artifacts_dir) / "at-1" / "host0-console.log").is_file()


def test_failed_provision_keeps_infra_when_asked(ready, fake):
    cfg = dataclasses.replace(ready, keep_infra_on_failure=True)
    fake.fail_install_of = "lm-acceptance-at-1-host1"
    with pytest.raises(LibvirtError):
        libvirt.provision(cfg, _user_data, fake)
    assert fake.domains == ["lm-acceptance-at-1-host0"]


def test_interrupted_provision_tears_down(ready, fake, monkeypatch):
    # Ctrl-C during the SSH wait: the fixture has not yielded, so nothing else cleans up.
    def interrupted(cfg, ip):
        raise KeyboardInterrupt

    monkeypatch.setattr(libvirt, "_wait_ssh", interrupted)
    with pytest.raises(KeyboardInterrupt):
        libvirt.provision(ready, _user_data, fake)
    assert fake.domains == []
    assert fake.nets == {}
