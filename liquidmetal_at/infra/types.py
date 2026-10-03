"""Backend-neutral infrastructure types shared by every provisioner.

A backend (DigitalOcean, libvirt) returns an :class:`Infra` holding an ordered list of
:class:`Node` — the flintlock hosts. Bootstrap, log collection and the tests depend only
on these types, never on a specific provisioner.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config

# Erlang distribution port range opened between nodes for the brigade mesh.
ERLANG_DIST_LOW = 9100
ERLANG_DIST_HIGH = 9200
EPMD_PORT = 4369
GOSSIP_UDP_PORT = 45892


@dataclass
class Node:
    """One flintlock host.

    ``public_ip`` is what the test process dials (SSH, brigade, poolmgrd); ``private_ip``
    is what the nodes use to reach each other. ``parent_iface`` is handed to
    ``flintlockd --parent-iface``; ``None`` means "use the default-route interface".
    """

    name: str
    public_ip: str
    private_ip: str
    parent_iface: str | None = None


@dataclass
class Infra:
    cfg: Config
    nodes: list[Node] = field(default_factory=list)
