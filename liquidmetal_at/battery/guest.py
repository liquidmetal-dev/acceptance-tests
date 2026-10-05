"""Check that a VM claimed from a battery pool can actually be used.

``ClaimVM`` returns a lease, the VM's uid and its host, but no guest address, and battery only
probes the guest agent before the VM first goes AVAILABLE. So a test that wants to know the
claimed VM works has to look for itself: ask the guest for its address over the vsock guest
agent on the hosting node, then SSH to that address through the node.
"""
from __future__ import annotations

import ipaddress
import logging
import shlex

from poolmgr.v1alpha1 import lease_pb2  # noqa: E402

from ..bootstrap.host import GUEST_DHCP_LEASES
from ..config import Config
from ..infra.types import Infra
from ..remote.bastion import ssh_to_microvm
from ..remote.ssh import SSH
from ..waiter import WaitTimeout, wait_until
from . import _flintlock  # noqa: F401

log = logging.getLogger("battery.guest")


def node_index(host_name: str) -> int:
    """battery's host names are ``host-<i>``, in ``Infra.nodes`` order."""
    return int(host_name.rsplit("-", 1)[1])


def _guest_ip(node_ssh: SSH, vm_uid: str) -> str:
    """The guest's own view of its eth1 address, read over the vsock guest agent.

    Retried briefly: an exec through the guest agent can come back with exit 0 and no output
    (seen once on a VM claimed seconds earlier), and the retry tells that apart from a guest
    that really has no address.
    """
    uds = shlex.quote(f"/run/flintlock/{vm_uid}/guest-agent.vsock")
    cmd = f"vsock-connect exec --uds {uds} --port 1024 -- ip -4 -o addr show eth1"

    def _read() -> str | None:
        rc, out, err = node_ssh.run(cmd, check=False)
        log.info("microVM %s eth1 over vsock: rc=%d out=%r err=%r", vm_uid, rc, out, err)
        # "3: eth1    inet 192.168.100.120/24 metric 100 brd ..."
        return out.split("inet ", 1)[1].split("/", 1)[0] if "inet " in out else None

    try:
        return wait_until(
            _read, timeout=30, interval=3, description=f"eth1 address of microVM {vm_uid}"
        )
    except WaitTimeout as exc:
        raise AssertionError(
            f"microVM {vm_uid} reported no IPv4 address on eth1 over the guest agent"
        ) from exc


def assert_claimed_vm_accessible(
    cfg: Config,
    infra: Infra,
    claimed: lease_pb2.ClaimVMResponse,
    *,
    expected_ip: str | None = None,
) -> str:
    """Fail unless the claimed VM answers over vsock and SSH; return its eth1 address.

    ``expected_ip`` is the address a static-template pool gave its VM. Without it the pool is
    taken to use the DHCP template: the address must then come from the hosting node's DHCP
    range and be in its lease file.
    """
    index = node_index(claimed.host.name)
    node_ssh = SSH(
        host=infra.nodes[index].public_ip, user="root", key_path=cfg.ssh_private_key_path
    )
    node_ssh.connect(timeout=cfg.timeout_ssh)
    try:
        ip = _guest_ip(node_ssh, claimed.vm_uid)

        if expected_ip is not None:
            assert ip == expected_ip, f"microVM {claimed.vm_uid} has {ip}, not {expected_ip}"
        else:
            first, last = (ipaddress.ip_address(a) for a in cfg.microvm_dhcp_range(index))
            assert first <= ipaddress.ip_address(ip) <= last, (
                f"{ip} is outside host {index}'s DHCP range"
            )
            _, leases, _ = node_ssh.run(f"cat {GUEST_DHCP_LEASES}")
            leased = {line.split()[2] for line in leases.splitlines() if line.strip()}
            assert ip in leased, f"{ip} has no DHCP lease on host {index}"

        vm = ssh_to_microvm(node_ssh, ip, cfg.ssh_private_key_path, timeout=cfg.timeout_ssh)
        try:
            # outbound through the host bridge's NAT
            vm.run("ping -c1 -W5 1.1.1.1")
        finally:
            vm.close()
        return ip
    finally:
        node_ssh.close()
