"""Quorum gating: with min_cluster_size=2, losing a node must refuse new placements.

Stops brigade on node B → node A becomes a size-1 partition → CreateMicroVM must be
refused (split-brain guard) while reads still succeed. Restarting B re-forms the mesh
and creates resume. Restores full cluster before finishing so later tests are unaffected.
"""
from __future__ import annotations

import logging

import grpc
import pytest

from liquidmetal_at import brigade_status
from liquidmetal_at.flintlock import spec
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

        # Writes must be refused.
        with pytest.raises(grpc.RpcError) as ei:
            req = spec.build_create_request(config, vm_index())
            fl_client.create(req)
        log.info("create correctly refused under partition: %s", ei.value.code())
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
