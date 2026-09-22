"""Lease expiry via the Sweeper: an un-heartbeated lease is deleted on expiry.

Confirmed directly from upstream (``cmd/poolmgrd/main.go``) that ``reconciler.NewSweeper``
is constructed and run - the repo's own runbook doc is stale on this point. Uses a short
per-pool ``heartbeat_expiry_threshold`` plus the suite's short BATTERY_SWEEP_INTERVAL /
BATTERY_WARNING_WINDOW so this doesn't need a long wait.
"""
from __future__ import annotations

import pytest
from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from liquidmetal_at.battery.spec import build_pool_spec
from liquidmetal_at.waiter import wait_until


@pytest.mark.e2e
def test_lease_expiry_deletes_and_replenishes(config, battery_client, hosts, vm_index):
    # No run_id prefix needed: the namespace already isolates the run.
    pool_name = "pool-expiry"
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.droplets))]

    spec = build_pool_spec(
        config,
        pool_name,
        index=vm_index(),
        size=1,
        flintlock_hosts=flintlock_hosts,
        replenishment_strategy=types_pb2.REPLACE_ON_DELETE,
        heartbeat_expiry_threshold_s=10,
    )
    battery_client.create_pool(spec)

    try:
        claimed = battery_client.wait_claimable(
            pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
        )
        assert claimed.lease_id in battery_client.lease_ids(pool_name, config.microvm_namespace)

        # Never heartbeat it - wait past heartbeat_expiry_threshold + the sweeper's own
        # sweep_interval/warning_window for it to notice and act. The sweeper deletes the
        # lease row on expiry, so ListLeases is the direct signal.
        wait_until(
            lambda: claimed.lease_id
            not in battery_client.lease_ids(pool_name, config.microvm_namespace),
            timeout=config.timeout_pool_available,
            interval=5,
            description=f"expired lease {claimed.lease_id} swept",
        )

        # REPLACE_ON_DELETE: the expired VM is deleted and replaced by a different one.
        replacement = battery_client.wait_claimable(
            pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
        )
        battery_client.release_vm(replacement.lease_id)
        assert replacement.vm_uid != claimed.vm_uid
    finally:
        battery_client.delete_pool(pool_name, config.microvm_namespace)
        try:
            battery_client.wait_deleted(
                pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
            )
        except Exception:  # noqa: BLE001
            pass
