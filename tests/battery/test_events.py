"""Events.Subscribe ordering: VM_PROVISIONED -> VM_AVAILABLE -> VM_CLAIMED -> VM_DELETED_ON_RELEASE.

Tolerant of interleaved POOL_REPLENISHING/POOL_SIZE_BELOW_TARGET events - same
tolerant-of-noise philosophy as ``liquidmetal_at.brigade_status``: this checks the expected
types appear as an in-order subsequence, not that the stream contains nothing else.
"""
from __future__ import annotations

import logging
import queue
import threading

import pytest
from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from liquidmetal_at.battery.spec import build_pool_spec
from liquidmetal_at.waiter import wait_until

log = logging.getLogger("test_events")

EventType = types_pb2.EventType


def _drain_events_in_background(
    client, pool_name: str, pool_namespace: str
) -> tuple[queue.Queue, threading.Event]:
    events: queue.Queue = queue.Queue()
    stop = threading.Event()

    def _run():
        try:
            for event in client.subscribe(pool_name, pool_namespace):
                events.put(event)
                if stop.is_set():
                    break
        except Exception as exc:  # noqa: BLE001 - stream torn down on client.close()
            log.debug("events stream ended: %s", exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return events, stop


def _seen_types(events: queue.Queue) -> list[int]:
    seen = []
    while True:
        try:
            seen.append(events.get_nowait().type)
        except queue.Empty:
            break
    return seen


def _subsequence_present(haystack: list[int], needle: list[int]) -> bool:
    it = iter(haystack)
    return all(any(x == n for x in it) for n in needle)


@pytest.mark.e2e
def test_events_subsequence(config, battery_client, hosts, vm_index):
    pool_name = f"{config.run_id}-pool-events"
    flintlock_hosts = [f"host-{i}" for i in range(len(hosts.droplets))]
    events, stop = _drain_events_in_background(
        battery_client, pool_name, config.microvm_namespace
    )

    try:
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
        battery_client.release_vm(claimed.lease_id)

        expected = [
            EventType.VM_PROVISIONED,
            EventType.VM_AVAILABLE,
            EventType.VM_CLAIMED,
            EventType.VM_DELETED_ON_RELEASE,
        ]

        seen_so_far: list[int] = []

        def _all_seen() -> list[int] | None:
            seen_so_far.extend(_seen_types(events))
            return list(seen_so_far) if _subsequence_present(seen_so_far, expected) else None

        seen = wait_until(
            _all_seen,
            timeout=config.timeout_pool_available,
            interval=3,
            description="expected event subsequence",
        )
        log.info("observed event types: %s", [EventType.Name(t) for t in seen])
    finally:
        stop.set()
        battery_client.delete_pool(pool_name, config.microvm_namespace)
        try:
            battery_client.wait_deleted(
                pool_name, config.microvm_namespace, timeout=config.timeout_pool_available
            )
        except Exception:  # noqa: BLE001
            pass
