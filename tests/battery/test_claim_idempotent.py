"""Idempotent ClaimVM: a repeated ``request_id`` replays the first claim (battery >= v0.4.0).

A client that retries a ClaimVM whose response it lost must get the lease it already holds
instead of a second VM, or an error from a size=1 pool that has nothing left to give.
"""
from __future__ import annotations

import pytest
from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from liquidmetal_at.battery.client import NoVMAvailable
from liquidmetal_at.battery.spec import build_pool_spec


@pytest.mark.e2e
def test_claim_with_same_request_id_replays_the_lease(config, battery_client, hosts, vm_index):
    # No run_id prefix needed: the namespace already isolates the run.
    pool_name = "pool-idem"
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.nodes))]
    request_id = f"{config.run_id}-claim-1"

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
        battery_client.wait_available(
            pool_name, config.microvm_namespace, 1, timeout=config.timeout_pool_available
        )

        first = battery_client.claim_vm(
            pool_name, config.microvm_namespace, request_id=request_id
        )
        replay = battery_client.claim_vm(
            pool_name, config.microvm_namespace, request_id=request_id
        )
        assert (replay.lease_id, replay.vm_uid) == (first.lease_id, first.vm_uid)
        assert replay.host.name == first.host.name

        leases = battery_client.list_leases(pool_name, config.microvm_namespace)
        assert [(lease.lease_id, lease.request_id) for lease in leases] == [
            (first.lease_id, request_id)
        ]

        # The replay did not take a second VM, and a different request_id is a new claim,
        # which this size=1 pool cannot satisfy while its only VM is leased.
        with pytest.raises(NoVMAvailable):
            battery_client.claim_vm(
                pool_name, config.microvm_namespace, request_id=f"{config.run_id}-claim-2"
            )
    finally:
        battery_client.delete_pool(pool_name, config.microvm_namespace, force=True)
        try:
            battery_client.wait_deleted(
                pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
            )
        except Exception:  # noqa: BLE001
            pass
