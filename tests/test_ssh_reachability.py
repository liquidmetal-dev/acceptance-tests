"""Running microVMs must be reachable by SSH (proving they actually booted + networked).

microVM IPs live on a host's internal bridge net, unreachable from the runner, so we
ProxyJump through the hosting droplet (bastion) to the VM's deterministic static IP.
Which host holds a given VM is scheduler-decided, so we try each droplet as bastion.
The guest's hostname (set via cloud-init) must equal the VM id.
"""
from __future__ import annotations

import logging

import pytest

from liquidmetal_at.flintlock import spec
from liquidmetal_at.flintlock.client import State
from liquidmetal_at.remote.bastion import ssh_to_microvm
from liquidmetal_at.remote.ssh import SSH

log = logging.getLogger("test_ssh")


def _ssh_via_any_host(config, cluster, microvm_ip: str):
    last_exc = None
    for droplet in cluster.droplets:
        bastion = SSH(host=droplet.public_ip, user="root", key_path=config.ssh_private_key_path)
        bastion.connect(timeout=config.timeout_ssh)
        try:
            vm = ssh_to_microvm(
                bastion, microvm_ip, config.ssh_private_key_path, timeout=30
            )
            return bastion, vm
        except Exception as exc:  # noqa: BLE001 - VM only lives behind one host
            last_exc = exc
            bastion.close()
    raise AssertionError(f"microVM {microvm_ip} unreachable via any host: {last_exc}")


@pytest.mark.e2e
def test_microvms_reachable_by_ssh(config, fl_client, cluster, vm_index):
    count = min(config.microvm_count, 2)
    vms: list[tuple[int, str]] = []  # (index, uid)
    for _ in range(count):
        idx = vm_index()
        vm = fl_client.create(spec.build_create_request(config, idx))
        vms.append((idx, vm.spec.uid))

    for _, uid in vms:
        fl_client.wait_state(uid, State.CREATED, timeout=config.timeout_vm_create)

    try:
        for idx, _uid in vms:
            target_ip = spec.static_ip_of(config, idx)
            expected_hostname = f"{config.run_id}-vm{idx}"
            bastion, guest = _ssh_via_any_host(config, cluster, target_ip)
            try:
                rc, out, _ = guest.run("hostname", check=True, timeout=30)
                log.info("microVM %s hostname=%s", target_ip, out.strip())
                assert rc == 0
                assert out.strip() == expected_hostname
                # prove we can run an arbitrary command too
                guest.run("uname -a", check=True, timeout=30)
            finally:
                guest.close()
                bastion.close()
    finally:
        for _, uid in vms:
            fl_client.delete(uid)
