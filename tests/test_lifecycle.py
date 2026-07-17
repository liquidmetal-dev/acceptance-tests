"""Happy-path microVM lifecycle through brigade: create -> get -> list -> delete.

Was blocked on 2 nodes by brigade's node-local Mnesia (GetMicroVM 404'd from the non-scheduler
node); fixed upstream by the Mnesia mesh-replication + singleton-failover work
(liquidmetal-dev/brigade#15 & #16; our reports #13 & #14). Should pass on latest brigade main.
"""
from __future__ import annotations

import pytest

from liquidmetal_at.flintlock import spec
from liquidmetal_at.flintlock.client import State


@pytest.mark.e2e
def test_microvm_lifecycle(config, fl_client, vm_index):
    idx = vm_index()
    req = spec.build_create_request(config, idx)

    created = fl_client.create(req)
    uid = created.spec.uid
    assert uid, "brigade did not return a uid on create"

    # reaches CREATED (nested virt is slow → generous timeout)
    vm = fl_client.wait_state(uid, State.CREATED, timeout=config.timeout_vm_create)
    assert vm.status.state == State.CREATED

    # Get by uid returns the same vm
    fetched = fl_client.get(uid)
    assert fetched.spec.uid == uid

    # List(namespace) includes it
    listed = fl_client.list(config.microvm_namespace)
    assert uid in {v.spec.uid for v in listed}

    # Delete → eventually gone
    fl_client.delete(uid)
    fl_client.wait_deleted(uid, timeout=config.timeout_vm_delete)
    assert not fl_client.exists(uid)
