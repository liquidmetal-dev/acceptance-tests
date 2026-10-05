"""Host cordon/uncordon via HostAdmin (battery >= v0.4.0).

A cordoned host gets no new VMs; the reconciler places them on the remaining hosts. With two
flintlock hosts, cordoning host-0 must put a new pool's VM on host-1 (left alone, every pool
in this suite lands on host-0). ``ClaimVMResponse.host`` is the only placement signal the API
gives for a single VM, see docs/battery-known-gaps.md.
"""
from __future__ import annotations

import pytest
from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from liquidmetal_at.battery.spec import build_pool_spec


@pytest.mark.e2e
def test_cordoned_host_gets_no_new_vms(config, battery_client, hosts, vm_index):
    if len(hosts.nodes) < 2:
        pytest.skip("needs NODE_COUNT >= 2: one host to cordon and one to take the VM")

    # No run_id prefix needed: the namespace already isolates the run.
    pool_name = "pool-cordon"
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.nodes))]
    cordoned, reason = "host-0", "acceptance test"

    assert {h.host.name for h in battery_client.list_hosts()} == set(flintlock_hosts)

    host = battery_client.cordon_host(cordoned, reason)
    try:
        assert host.cordoned
        by_name = {h.host.name: h for h in battery_client.list_hosts()}
        assert by_name[cordoned].host.cordoned
        assert by_name[cordoned].host.cordoned_reason == reason
        assert not by_name["host-1"].host.cordoned

        spec = build_pool_spec(
            config,
            pool_name,
            index=vm_index(),
            size=1,
            flintlock_hosts=flintlock_hosts,
            replenishment_strategy=types_pb2.REPLACE_ON_DELETE,
        )
        battery_client.create_pool(spec)

        claimed = battery_client.wait_claimable(
            pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
        )
        assert claimed.host.name != cordoned

        by_name = {h.host.name: h for h in battery_client.list_hosts()}
        assert by_name[claimed.host.name].vm_count >= 1
    finally:
        try:
            if battery_client.pool_exists(pool_name, config.microvm_namespace):
                battery_client.delete_pool(pool_name, config.microvm_namespace, force=True)
        finally:
            # leave the host usable for whatever test runs next
            battery_client.uncordon_host(cordoned)

    by_name = {h.host.name: h for h in battery_client.list_hosts()}
    assert not by_name[cordoned].host.cordoned
