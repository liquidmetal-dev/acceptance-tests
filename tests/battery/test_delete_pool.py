"""DeletePool with a leased VM: refused without ``force``, drained with it.

Since v0.4.0 (battery#112) DeletePool deletes a pool's unleased VMs itself, which
test_pool_lifecycle and test_claim_release cover. A leased VM is the one case it still
refuses, with FAILED_PRECONDITION, unless ``DeletePoolRequest.force`` is set; a forced delete
ends the lease, so its holder gets NOT_FOUND on the next Heartbeat.
"""
from __future__ import annotations

import grpc
import pytest
from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from liquidmetal_at.battery.spec import build_pool_spec


@pytest.mark.e2e
def test_delete_pool_with_leased_vm_needs_force(config, battery_client, hosts, vm_index):
    # No run_id prefix needed: the namespace already isolates the run.
    pool_name = "pool-delete"
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.nodes))]

    spec = build_pool_spec(
        config,
        pool_name,
        index=vm_index(),
        size=1,
        flintlock_hosts=flintlock_hosts,
        replenishment_strategy=types_pb2.REPLACE_ON_DELETE,
    )
    battery_client.create_pool(spec)

    try:
        claimed = battery_client.wait_claimable(
            pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
        )

        with pytest.raises(grpc.RpcError) as excinfo:
            battery_client.delete_pool(pool_name, config.microvm_namespace)
        assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        # The refused delete changed nothing: pool, lease and VM are all still there.
        pool = battery_client.get_pool(pool_name, config.microvm_namespace)
        assert pool.status.leased_count == 1
        assert battery_client.heartbeat(claimed.lease_id).expires_at.ToDatetime()

        battery_client.delete_pool(pool_name, config.microvm_namespace, force=True)
        battery_client.wait_deleted(
            pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
        )

        with pytest.raises(grpc.RpcError) as excinfo:
            battery_client.heartbeat(claimed.lease_id)
        assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
        assert claimed.lease_id not in {lease.lease_id for lease in battery_client.list_leases()}
    finally:
        if battery_client.pool_exists(pool_name, config.microvm_namespace):
            battery_client.delete_pool(pool_name, config.microvm_namespace, force=True)
