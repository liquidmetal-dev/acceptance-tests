"""A pool of more than one VM: it fills, spreads across hosts, and each VM is on the network.

battery clones the pool's ``microvm_template`` for every VM and rejects one with a static
address or ``guest_mac`` once the pool can hold two VMs (see test_pool_validation). So this
pool uses the DHCP template (``build_pool_spec(..., network="dhcp")``), and each battery host
serves DHCP on its flintlock bridge from its own range (``Config.microvm_dhcp_range``).

The pool is sized to one VM per host on purpose. A guest with no ``guest_mac`` derives its
own MAC, and guests booted from the same template derive the same one (seen:
``fa:90:93:1a:a6:c1`` on both hosts), so two pool VMs on one bridge would share a MAC and a
lease. battery's host picker places each VM on the host with the fewest VMs for the pool, so
``size == number of hosts`` gives exactly one per host. See docs/battery-known-gaps.md.

``ClaimVM`` reports no guest address (the MAC it returns is the host-side TAP device's), so
the test asks the guest over the vsock guest-agent, then checks the answer against the host's
DHCP leases and by SSH through the host.
"""
from __future__ import annotations

import pytest
from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from liquidmetal_at.battery.guest import assert_claimed_vm_accessible
from liquidmetal_at.battery.spec import build_pool_spec


@pytest.mark.e2e
def test_multi_vm_pool_spreads_and_each_vm_is_on_the_network(config, battery_client, hosts):
    if config.microvm_provider != "firecracker":
        pytest.skip("the DHCP template matches the guest NIC by name, which needs firecracker")
    if len(hosts.nodes) < 2:
        pytest.skip("needs NODE_COUNT >= 2: the pool holds one VM per host")

    # No run_id prefix needed: the namespace already isolates the run.
    pool_name = "pool-multi"
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.nodes))]
    size = len(flintlock_hosts)

    spec = build_pool_spec(
        config,
        pool_name,
        size=size,
        flintlock_hosts=flintlock_hosts,
        replenishment_strategy=types_pb2.REPLACE_ON_DELETE,
        network="dhcp",
    )
    battery_client.create_pool(spec)

    try:
        ready = battery_client.wait_available(
            pool_name, config.microvm_namespace, size, timeout=config.timeout_pool_available
        )
        assert ready.status.available_count == size

        claims = [battery_client.claim_vm(pool_name, config.microvm_namespace) for _ in range(size)]
        assert len({c.vm_uid for c in claims}) == size
        # one VM per host
        assert {c.host.name for c in claims} == set(flintlock_hosts)

        # no expected_ip: each address must come from its host's DHCP range and lease file
        ips = [assert_claimed_vm_accessible(config, hosts, c) for c in claims]
        assert len(set(ips)) == size

        # Releasing one VM deletes it; its replacement goes to the host that is now short.
        released = claims[0]
        battery_client.release_vm(released.lease_id)
        replacement = battery_client.wait_claimable(
            pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
        )
        assert replacement.vm_uid != released.vm_uid
        assert replacement.host.name == released.host.name
        assert_claimed_vm_accessible(config, hosts, replacement)
        pool = battery_client.get_pool(pool_name, config.microvm_namespace)
        assert pool.status.leased_count == size
    finally:
        battery_client.delete_pool(pool_name, config.microvm_namespace, force=True)
