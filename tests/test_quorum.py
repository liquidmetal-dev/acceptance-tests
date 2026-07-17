"""Quorum gating: with min_cluster_size=2, losing a node must refuse new placements.

Stops brigade on node B → node A becomes a size-1 partition → CreateMicroVM must be
refused (split-brain guard) while reads still succeed. Restarting B re-forms the mesh
and creates resume. Restores full cluster before finishing so later tests are unaffected.

Was blocked by an unstable scheduler singleton (:noproc → UNKNOWN "Internal Server Error"); fixed
upstream by self-healing singleton failover + typed UNAVAILABLE (liquidmetal-dev/brigade#16; our
report #14). The refusal now returns UNAVAILABLE, which the client wraps as
_TransientSchedulerError.
"""
from __future__ import annotations

import logging

import pytest

from liquidmetal_at import brigade_status
from liquidmetal_at.flintlock import spec
from liquidmetal_at.flintlock.client import _TransientSchedulerError
from liquidmetal_at.remote.ssh import SSH
from liquidmetal_at.waiter import wait_until

log = logging.getLogger("test_quorum")


def _brigade(config, droplet, action: str) -> None:
    ssh = SSH(host=droplet.public_ip, user="root", key_path=config.ssh_private_key_path)
    ssh.connect(timeout=config.timeout_ssh)
    try:
        ssh.sudo(f"systemctl {action} brigade")
    finally:
        ssh.close()


@pytest.mark.e2e
def test_quorum_gates_placement(config, fl_client, cluster, vm_index):
    if config.brigade_min_cluster_size < 2 or len(cluster.droplets) < 2:
        pytest.skip("quorum test requires min_cluster_size>=2 and 2 nodes")

    node_a, node_b = cluster.droplets[0], cluster.droplets[1]

    # Break quorum: stop brigade on node B.
    _brigade(config, node_b, "stop")
    try:
        wait_until(
            lambda: brigade_status.cluster_size(node_a.public_ip, config.brigade_status_port) < 2,
            timeout=config.timeout_cluster,
            interval=3,
            description="node A sees partition (size<2)",
        )

        # Reads still work under partition.
        fl_client.list(config.microvm_namespace)

        # Writes must be refused. brigade returns UNAVAILABLE ("scheduler partition lacks
        # quorum; placement refused"), which the client classes as a transient scheduler
        # code and wraps as _TransientSchedulerError. Under a real partition the refusal is
        # persistent (node B stays down), so retrying is futile — use retry_timeout=0 for a
        # single attempt and assert the wrapped refusal surfaces immediately.
        with pytest.raises(_TransientSchedulerError) as ei:
            req = spec.build_create_request(config, vm_index())
            fl_client.create(req, retry_timeout=0)
        log.info("create correctly refused under partition: %s", ei.value)
    finally:
        # Heal the cluster regardless of assertion outcome.
        _brigade(config, node_b, "start")

    wait_until(
        lambda: brigade_status.cluster_size(node_a.public_ip, config.brigade_status_port) >= 2,
        timeout=config.timeout_cluster,
        interval=3,
        description="cluster re-forms (size>=2)",
    )

    # Creates resume once quorum is restored.
    vm = fl_client.create(spec.build_create_request(config, vm_index()))
    assert vm.spec.uid
    fl_client.delete(vm.spec.uid)
    # Wait out the delete so the single schedulable host is free for later tests.
    try:
        fl_client.wait_deleted(vm.spec.uid, timeout=config.timeout_vm_delete)
    except Exception:  # noqa: BLE001
        pass
