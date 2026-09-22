"""Claim / heartbeat / release, and replenishment after release.

Uses REPLACE_ON_DELETE so releasing the claimed VM triggers exactly one replacement,
matching docs/runbooks/e2e-manual-verification.md step 8 in the upstream repo. The pool's
first VM comes from the reconciler's start-of-life seed (battery >= v0.3.2); before that an
event-driven pool never provisioned anything, since nothing could be claimed or deleted.
"""
from __future__ import annotations

import pytest
from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from liquidmetal_at.battery.client import NoVMAvailable
from liquidmetal_at.battery.spec import build_pool_spec


@pytest.mark.e2e
def test_claim_heartbeat_release_replenishes(config, battery_client, hosts, vm_index):
    # No run_id prefix: the namespace already isolates the run, and the pool name is
    # part of a length-limited socket path (see battery/spec.py).
    pool_name = "pool-claim"
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.droplets))]

    spec = build_pool_spec(
        config,
        pool_name,
        index=vm_index(),
        size=1,
        flintlock_hosts=flintlock_hosts,
        replenishment_strategy=types_pb2.REPLACE_ON_DELETE,
    )
    battery_client.create_pool(spec)

    # Before any VM is AVAILABLE, ClaimVM must be refused (RESOURCE_EXHAUSTED).
    with pytest.raises(NoVMAvailable):
        battery_client.claim_vm(pool_name, config.microvm_namespace)

    claimed = battery_client.wait_claimable(
        pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
    )
    assert claimed.lease_id
    assert claimed.vm_uid
    assert claimed.host.address

    leases = battery_client.list_leases(pool_name, config.microvm_namespace)
    assert [(lease.lease_id, lease.vm_uid) for lease in leases] == [
        (claimed.lease_id, claimed.vm_uid)
    ]

    hb = battery_client.heartbeat(claimed.lease_id)
    assert hb.expires_at.ToDatetime()

    battery_client.release_vm(claimed.lease_id)
    assert claimed.lease_id not in battery_client.lease_ids(pool_name, config.microvm_namespace)

    # REPLACE_ON_DELETE: the reconciler provisions exactly one replacement.
    battery_client.wait_available(
        pool_name, config.microvm_namespace, 1, timeout=config.timeout_pool_available
    )

    battery_client.delete_pool(pool_name, config.microvm_namespace)
    battery_client.wait_deleted(
        pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
    )
