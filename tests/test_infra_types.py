"""Offline checks for the backend-neutral infra types and their use by DO + templates."""
from __future__ import annotations

from liquidmetal_at.bootstrap.render import render
from liquidmetal_at.infra import do
from liquidmetal_at.infra.types import Infra, Node


class _FakeDroplets:
    def get(self, droplet_id):
        return {
            "droplet": {
                "status": "active",
                "name": "lm-acceptance-at-1-host0",
                "networks": {
                    "v4": [
                        {"type": "public", "ip_address": "203.0.113.5"},
                        {"type": "private", "ip_address": "10.0.0.2"},
                    ]
                },
            }
        }


class _FakeClient:
    droplets = _FakeDroplets()


def test_do_droplet_becomes_node_with_vpc_parent_iface():
    node = do._droplet_ready(_FakeClient(), 7)
    assert node == Node(
        name="lm-acceptance-at-1-host0",
        public_ip="203.0.113.5",
        private_ip="10.0.0.2",
        parent_iface="eth1",
    )


def test_do_infra_is_an_infra():
    infra = do.DOInfra(cfg=None, client=_FakeClient())
    assert isinstance(infra, Infra)
    assert infra.nodes == []


def _host_script(parent_iface):
    return render(
        "provision_host.sh.j2",
        thinpool="tp",
        parent_iface=parent_iface,
        bridge_name="flintlock0",
        bridge_addr="192.168.100.1/24",
        guest_subnet="192.168.100.0/24",
        flintlock_grpc_port=9090,
    )


def test_host_script_uses_explicit_parent_iface():
    script = _host_script("eth1")
    assert "PARENT_IFACE=eth1\n" in script
    assert "DISK=" not in script


def test_host_script_detects_parent_iface_when_unset():
    script = _host_script(None)
    assert "PARENT_IFACE=$(ip route show default" in script
    assert "PARENT_IFACE=None" not in script
    assert "no default-route interface" in script
