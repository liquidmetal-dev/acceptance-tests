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
    assert fake.vols == [name]
    assert len(fake.ran("vol-upload")) == 1

    libvirt.ensure_base_image(cfg, fake)  # second run reuses the volume
    assert len(fake.ran("vol-upload")) == 1


def test_checksum_mismatch_raises_and_caches_nothing(tmp_path, fake, monkeypatch):
    url, _ = _image(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path, libvirt_base_image_url=url, libvirt_base_image_sha256="0" * 64)
    with pytest.raises(LibvirtError, match="checksum"):
        libvirt.ensure_base_image(cfg, fake)
    assert list((tmp_path / "cache").iterdir()) == []
    assert fake.vols == []


def test_failed_upload_removes_partial_base_volume(tmp_path, fake, monkeypatch):
    url, sha = _image(tmp_path, monkeypatch)
    cfg = _cfg(tmp_path, libvirt_base_image_url=url, libvirt_base_image_sha256=sha)
    fake.fail["vol-upload"] = "connection reset"
    with pytest.raises(LibvirtError, match="connection reset"):
        libvirt.ensure_base_image(cfg, fake)
    assert fake.vols == []
