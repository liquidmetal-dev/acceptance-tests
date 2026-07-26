"""A microVM's guest-agent must be reachable over vsock from its flintlock host.

flintlock (>= v0.11.0) attaches an AF_VSOCK device when the spec sets ``allow_guest_agent``
(host CID 2 <-> guest CID 3) and reports the host-side unix socket in
``MicroVMStatus.vsock_path``. The in-guest guest-agent (installed here via apt during
cloud-init) listens on that vsock (control port 1024); the host drives it with the
``vsock-connect`` CLI.

vsock is host-local, so the runner cannot reach guest CID 3 directly — the per-VM UDS only
exists on the droplet actually running the VM. So we read ``vsock_path`` back through brigade
(which forwards GetMicroVM to that host verbatim), find which droplet holds the socket, and run
``vsock-connect exec`` there over SSH. This proves the vsock control channel end to end — a
different path than :mod:`tests.test_ssh_reachability`, which reaches the guest over the network.

``vsock_path`` and ``allow_guest_agent`` are newer flintlock proto fields absent from brigade's
own (older) proto, but brigade forwards the decoded request/response structs and Elixir's
protobuf preserves unknown fields across re-encode, so both ride through brigade untouched.
"""
from __future__ import annotations

import logging
import shlex
import time

import pytest

from liquidmetal_at import logs
from liquidmetal_at.flintlock import spec
from liquidmetal_at.flintlock.client import State
from liquidmetal_at.remote.ssh import SSH

log = logging.getLogger("test_guest_agent")


def _wait_vsock_path(fl_client, uid: str, *, timeout: float) -> str:
    """Poll GetMicroVM until brigade reports a non-empty vsock_path.

    The path is populated once flintlock has wired the vsock device, which can lag the CREATED
    state by a few seconds. An empty path past the deadline means either the flintlockd under
    test predates guest-agent support or brigade dropped the field — both worth failing loudly.
    """
    deadline = time.monotonic() + timeout
    while True:
        path = fl_client.get(uid).status.vsock_path
        if path:
            return path
        if time.monotonic() >= deadline:
            raise AssertionError(f"microvm {uid} never reported a vsock_path")
        time.sleep(3)


def _host_with_agent(config, cluster, vsock_path: str):
    """Return a connected SSH to whichever droplet's guest-agent answers on vsock_path.

    The UDS is created by flintlockd on the host running the VM, so it exists on exactly one
    droplet — which host is scheduler-decided. The guest-agent inside the guest only starts
    listening after the guest boots + cloud-init installs it, so ``ping`` can fail for up to a
    minute+; cycle every host under a generous deadline rather than trying each once.
    """
    quoted = shlex.quote(vsock_path)
    deadline = time.monotonic() + config.timeout_vm_create
    last = None
    while True:
        for droplet in cluster.droplets:
            ssh = SSH(host=droplet.public_ip, user="root", key_path=config.ssh_private_key_path)
            ssh.connect(timeout=config.timeout_ssh)
            try:
                rc, _, _ = ssh.run(f"test -S {quoted}", check=False, timeout=15)
                if rc == 0:
                    rc, out, err = ssh.run(
                        f"vsock-connect ping --uds {quoted} --port 1024",
                        check=False,
                        timeout=30,
                    )
                    if rc == 0:
                        return ssh
                    last = f"ping rc={rc}: {(err or out).strip()}"
            except Exception as exc:  # noqa: BLE001 - agent lives behind one host
                last = str(exc)
            ssh.close()
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"guest-agent at {vsock_path} unreachable via any host: {last}"
            )
        time.sleep(5)


@pytest.mark.e2e
def test_guest_agent_reachable_over_vsock(config, fl_client, cluster, vm_index):
    idx = vm_index()
    vm = fl_client.create(spec.build_create_request(config, idx, guest_agent=True))
    uid = vm.spec.uid
    try:
        fl_client.wait_state(uid, State.CREATED, timeout=config.timeout_vm_create)
        vsock_path = _wait_vsock_path(fl_client, uid, timeout=60)
        log.info("microVM %s vsock_path=%s", uid, vsock_path)

        quoted = shlex.quote(vsock_path)
        base = f"vsock-connect exec --uds {quoted} --port 1024 --"
        host = _host_with_agent(config, cluster, vsock_path)
        try:
            # cloud-init set the guest hostname to the VM id — read it back over vsock.
            rc, out, _ = host.run(f"{base} hostname", check=True, timeout=30)
            assert rc == 0
            assert out.strip() == f"{config.run_id}-vm{idx}"
            # An arbitrary command also works.
            host.run(f"{base} uname -a", check=True, timeout=30)
            # Exit codes propagate through vsock-connect (guest `false` -> non-zero).
            rc, _, _ = host.run(f"{base} false", check=False, timeout=30)
            assert rc != 0, "vsock-connect did not propagate the guest's non-zero exit code"
            # The agent's own info endpoint answers.
            host.run(f"vsock-connect info --uds {quoted} --port 1024", check=True, timeout=30)
        finally:
            host.close()
    finally:
        # Snapshot the guest serial console before delete so a guest-side failure (e.g. missing
        # virtio-vsock, or cloud-init apt install) is diagnosable after teardown. Best-effort.
        try:
            logs.collect_vm_consoles(config, cluster.droplets, tag="guest-agent-vsock")
        except Exception:  # noqa: BLE001
            pass
        fl_client.delete(uid)
