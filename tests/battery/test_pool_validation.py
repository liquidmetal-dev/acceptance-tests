"""CreatePool rejects a static-network template that would put two VMs on one address.

battery clones the pool's ``microvm_template`` for every VM, static IP and ``guest_mac``
included. Since v0.4.0 (battery#114) CreatePool returns INVALID_ARGUMENT when the spec lets
such a pool hold more than one VM, instead of letting the addresses collide. Our template
(``liquidmetal_at/flintlock/spec.py``) carries both, so it is the rejected shape. No VM is
provisioned here. See docs/battery-known-gaps.md.
"""
from __future__ import annotations

import grpc
import pytest
from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from liquidmetal_at.battery.spec import build_pool_spec


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("pool_name", "size", "strategy"),
    [
        pytest.param("pool-invalid-size", 2, types_pb2.REPLACE_ON_DELETE, id="size-2"),
        # provisions a new VM on every claim without counting the leased one
        pytest.param(
            "pool-invalid-iol", 1, types_pb2.IMMEDIATE_ON_LEASE, id="immediate-on-lease"
        ),
    ],
)
def test_static_template_rejected_when_pool_can_hold_two_vms(
    config, battery_client, hosts, vm_index, pool_name, size, strategy
):
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.nodes))]
    spec = build_pool_spec(
        config,
        pool_name,
        index=vm_index(),
        size=size,
        flintlock_hosts=flintlock_hosts,
        replenishment_strategy=strategy,
    )

    try:
        with pytest.raises(grpc.RpcError) as excinfo:
            battery_client.create_pool(spec)
        assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        # the error names the offending template field
        assert "microvm_template.interfaces" in excinfo.value.details()
        assert not battery_client.pool_exists(pool_name, config.microvm_namespace)
    finally:
        # only reached with a pool to remove if battery wrongly accepted the spec
        if battery_client.pool_exists(pool_name, config.microvm_namespace):
            battery_client.delete_pool(pool_name, config.microvm_namespace, force=True)
