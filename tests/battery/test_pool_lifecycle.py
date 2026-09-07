"""Happy-path pool lifecycle: create -> reconciler provisions -> get/list -> delete.

Pool size is deliberately 1 - verified directly from upstream
(``internal/reconciler/provision.go``) that the reconciler clones ``microvm_template``
byte-for-byte for every VM in the pool (only ``allow_guest_agent`` is overridden
server-side), so a template carrying a static IP (as ours does, see
``liquidmetal_at/flintlock/spec.py``) is only safe for size=1 pools. See
docs/battery-known-gaps.md.
"""
from __future__ import annotations

import pytest

from liquidmetal_at.battery import metrics_status
from liquidmetal_at.battery.spec import build_pool_spec


@pytest.mark.e2e
def test_pool_lifecycle(config, battery_client, poolmgrd_node, hosts, vm_index):
    pool_name = f"{config.run_id}-pool-lifecycle"
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.droplets))]

    spec = build_pool_spec(
        config,
        pool_name,
        index=vm_index(),
        size=1,
        flintlock_hosts=flintlock_hosts,
    )
    pool = battery_client.create_pool(spec)
    assert pool.spec.name == pool_name

    # reconciler provisions up to size=1 (nested virt is slow -> generous timeout)
    ready = battery_client.wait_available(
        pool_name, config.microvm_namespace, 1, timeout=config.timeout_pool_available
    )
    assert ready.status.available_count >= 1
    assert ready.status.leased_count == 0

    fetched = battery_client.get_pool(pool_name, config.microvm_namespace)
    assert fetched.spec.name == pool_name

    listed = battery_client.list_pools(config.microvm_namespace)
    assert pool_name in {p.spec.name for p in listed}

    # Cross-check against poolmgrd's own /metrics as an independent ground truth.
    metrics = metrics_status.pool_status(
        poolmgrd_node.public_ip, config.battery_metrics_port, pool_name, config.microvm_namespace
    )
    if metrics:  # metrics is best-effort/tolerant - only assert when the gauges are present
        assert metrics.get("available") == fetched.status.available_count

    battery_client.delete_pool(pool_name, config.microvm_namespace)
    battery_client.wait_deleted(
        pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
    )
    assert not battery_client.pool_exists(pool_name, config.microvm_namespace)
