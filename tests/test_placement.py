"""Placement spread: N microVMs must schedule across BOTH flintlock hosts.

Host capacity is sized (in bootstrap) so a single host tops out at N-1 VMs, forcing
the scheduler to use both. Authoritative check is per-host flintlock state on disk
(``/var/lib/flintlock/vm/<ns>/``), which is independent of the brigade /status schema;
we also opportunistically assert via brigade's placement map when it is parseable.
"""
from __future__ import annotations

import logging

import pytest

from liquidmetal_at import brigade_status
from liquidmetal_at.flintlock import spec
from liquidmetal_at.flintlock.client import State
from liquidmetal_at.remote.ssh import SSH

log = logging.getLogger("test_placement")

FLINTLOCK_STATE_DIR = "/var/lib/flintlock/vm"


def _ids_on_host(config, droplet) -> set[str]:
    ssh = SSH(host=droplet.public_ip, user="root", key_path=config.ssh_private_key_path)
    ssh.connect(timeout=config.timeout_ssh)
    try:
        ns_dir = f"{FLINTLOCK_STATE_DIR}/{config.microvm_namespace}"
        _, out, _ = ssh.run(
            f"find {ns_dir} -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null || true",
            check=False,
        )
        return {line.strip() for line in out.splitlines() if line.strip()}
    finally:
        ssh.close()


@pytest.mark.e2e
def test_placement_spreads_across_hosts(config, fl_client, cluster, vm_index):
    n = config.microvm_count
    assert n >= 2, "placement test needs MICROVM_COUNT >= 2"

    created = []
    for _ in range(n):
        idx = vm_index()
        vm = fl_client.create(spec.build_create_request(config, idx))
        created.append(vm.spec.uid)

    for uid in created:
        fl_client.wait_state(uid, State.CREATED, timeout=config.timeout_vm_create)

    all_ids = {v.spec.id for v in fl_client.list(config.microvm_namespace)}
    our_ids = {vm_id for vm_id in all_ids if vm_id.startswith(config.run_id)}
    assert len(our_ids) >= n

    # Authoritative spread check via per-host flintlock state.
    per_host = {d.public_ip: _ids_on_host(config, d) for d in cluster.droplets}
    for ip, ids in per_host.items():
        log.info("host %s holds %d microvms: %s", ip, len(ids), sorted(ids))

    hosting = [ip for ip, ids in per_host.items() if ids & our_ids]
    assert len(hosting) == len(cluster.droplets), (
        f"expected microVMs on all {len(cluster.droplets)} hosts, got {len(hosting)}: {per_host}"
    )

    # No VM double-placed across hosts.
    host_sets = [ids & our_ids for ids in per_host.values()]
    for i in range(len(host_sets)):
        for j in range(i + 1, len(host_sets)):
            assert not (host_sets[i] & host_sets[j]), "a microVM appears on two hosts"

    # Union covers all our VMs.
    union = set().union(*host_sets) if host_sets else set()
    assert our_ids <= union

    # Opportunistic brigade /status cross-check (tolerant; skip if schema unknown).
    try:
        pmap = brigade_status.placement_map(
            cluster.droplets[0].public_ip, config.brigade_status_port
        )
        if pmap:
            assert len({h for u, h in pmap.items() if u in created}) >= 2
    except Exception as exc:  # noqa: BLE001
        log.info("brigade /status placement map unavailable (%s); relied on host state", exc)

    for uid in created:
        fl_client.delete(uid)
