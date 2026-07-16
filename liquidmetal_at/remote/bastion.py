"""ProxyJump SSH to microVMs whose IPs live on a host's internal bridge net.

microVM IPs (e.g. 192.168.100.x) are only reachable from their flintlock host, not
from the test runner. ``ssh_to_microvm`` opens a ``direct-tcpip`` channel through an
already-connected host :class:`SSH` (the bastion) and tunnels a second SSH session to
the microVM over it — the paramiko equivalent of ``ssh -J host microvm``.
"""
from __future__ import annotations

import logging

import paramiko

from ..waiter import retry_call
from .ssh import SSH

log = logging.getLogger("bastion")


def ssh_to_microvm(
    bastion: SSH,
    microvm_ip: str,
    key_path: str,
    *,
    user: str = "root",
    timeout: float,
) -> SSH:
    """Return a connected :class:`SSH` to ``microvm_ip`` tunnelled through ``bastion``.

    Retries until the guest's sshd is up (guest cloud-init + sshd start lag behind the
    microVM reaching CREATED state).
    """

    def _dial() -> SSH:
        chan = bastion.open_direct_channel(microvm_ip, 22)
        vm = SSH(host=microvm_ip, user=user, key_path=key_path)
        vm._sock = chan
        # single-shot connect; retry handled by the outer retry_call
        vm._client = vm._new_client()
        return vm

    return retry_call(
        _dial,
        timeout=timeout,
        exceptions=(OSError, paramiko.SSHException, EOFError),
        description=f"ssh -J {bastion.host} root@{microvm_ip}",
    )
