# Upstream brigade findings (blocking the 2-node acceptance suite)

The acceptance suite provisions a **2-node** brigade cluster (2 DigitalOcean droplets, each a
flintlock host). Against `brigade@main` (topology A / M1) the multi-node paths do not work
coherently. This documents the two root causes, the evidence, and which suite tests they block,
so they can be filed upstream at https://github.com/liquidmetal-dev/brigade.

**Filed upstream — both FIXED in brigade main (post-`5a08280`):**
- Bug 1 → issue #13 → fixed by liquidmetal-dev/brigade#15 `fix(store): replicate Mnesia tables across the mesh` (`f16e991`)
- Bug 2 → issue #14 → fixed by liquidmetal-dev/brigade#16 `fix(scheduler): self-heal singleton failover + typed UNAVAILABLE` (`fc08054`)

As of brigade `4a61dc0` (2026-07-17) these are resolved; the suite runs against `--branch main`, so
`make test` exercises the fixes. **Final result: `make test` is fully green — 15 passed, 0 failed.**

Two *suite-side* config fixes were also required once brigade could actually place across nodes:

1. **`brigade_config.exs.j2` `flintlock_endpoint` = the node's private IP, not `localhost`.**
   `SelfRegister` records this as the host's endpoint in the (now-replicated) store, and the
   scheduler node dials it via `HostDriver.Local` to place a VM on that host. With `localhost`,
   every host resolved to the scheduler node's own flintlock, so all VMs piled onto one host even
   though `schedulable_hosts: 2`. flintlockd listens on `0.0.0.0:9090`, so the private IP is
   reachable cross-node. `host.py` now passes `private_ip` into the render.
2. **`test_ssh_reachability` retries reachability patiently** (cycles both hosts under a
   `timeout_vm_create` deadline). The guest OS boots *after* the microVM reaches CREATED, so its
   static IP + sshd lag by up to a minute on nested virt; trying each host once gave "No route to
   host". `test_placement`'s `xfail` was removed (spread works now) and the KNOWN-BLOCKER docstrings
   were updated. The rest of this doc is the original diagnosis, kept for history.

Offline tests and the single-VM happy path only pass when the scheduler singleton happens to land
on the node the client dials — which is a per-run coin flip (see Bug 2). Per the project owner's
decision, the affected e2e tests are **left failing (not skipped)**; they are the suite honestly
reporting that brigade is not yet multi-node-ready. Re-enable/clean up once the fixes below land.

> **Version pinning:** `brigade` and `flintlock` `main` are re-cloned on every run, so the exact
> commit under test drifts. `logs.py` now records `brigade=` / `flintlock=` short SHAs in each
> run's `host*-diag.txt` (`== sut-versions ==`). Quote those SHAs when filing. Observed with
> flintlockd `v0.9.0` (commit `a7c62f3`); brigade `main` around 2026-07-17.

---

## Bug 1 — Mnesia host/VM tables are node-local (never replicated across the mesh)

`lib/brigade/store/mnesia.ex` creates both tables with `ram_copies: [node()]` and there is **no**
cross-node join anywhere (`grep` for `add_table_copy`, `extra_db_nodes`, `change_config`, schema
merge on `nodeup` — none exist). The moduledoc claims the tables are *"replicated across the mesh"*
but the code makes each node's Mnesia an isolated island.

```elixir
# lib/brigade/store/mnesia.ex (setup!/0)
create_table(@vms,   attributes: [...], index: [...], ram_copies: [node()])
create_table(@hosts, attributes: [...],                ram_copies: [node()])
```

`Brigade.HostRegistry.SelfRegister` registers **only the local node's** host into the local table;
the scheduler's `handle_info({:nodeup, up}, ...)` is a documented no-op ("Nothing to do here").
Cross-node host aggregation ("shared registry provider", nodeup-driven re-registration) is deferred
to **M3** per the moduledocs in `host_registry.ex` / `self_register.ex`.

**Two observable consequences:**

1. **Host aggregation never happens.** `/status` reports `schedulable_hosts: 1` and a `hosts` array
   containing only the scheduler node's own host, even though `partition.size: 2` and
   `in_quorum: true`. The scheduler packs every VM onto the one visible host and refuses the (N)th
   with `RESOURCE_EXHAUSTED: "no host has capacity"`. The other host runs zero VMs (0-byte
   firecracker log). → blocks **multi-host placement spread**.

2. **VM records are invisible from the non-scheduler node.** The singleton writes VM records into
   *its* node's Mnesia. A client dialing the other node's north edge gets
   `NOT_FOUND` from `GetMicroVM` for a VM that was just created successfully. → blocks the
   **basic CRUD lifecycle** whenever the client and the scheduler are on different nodes.

**Evidence (`artifacts/at-5dd36efd`, `artifacts/at-d25bc86b`):**
```
/status: "hosts":[{ "id":"brigade@10.106.32.2" ... }], "schedulable_hosts":1, "partition":{"size":2,"in_quorum":true}
brigade: (GRPC.RPCError) no host has capacity for 1 vcpu / 1024 MB   # 4th VM, host0 full, host1 invisible
brigade: (GRPC.RPCError) microvm 01KXQQBFE02W6VYWBZP4R8NB7S not found # GetMicroVM from the node that isn't the scheduler
```

**Fix direction:** replicate the tables across the mesh — coordinated schema merge
(`:mnesia.change_config(:extra_db_nodes, Node.list())`) + table copies on join, or land the M3
shared-registry provider so every node's scheduler sees every host and every VM record.

---

## Bug 2 — the scheduler singleton is not stable on a 2-node cluster

The scheduler is a Horde singleton registered as `{Brigade.Scheduler.Registry, :singleton}`.
`CreateMicroVM` does `GenServer.call(singleton, {:reserve, ...})`. Mid-run the singleton was **not
alive on any node** — the call exited with `:noproc`, and brigade returned
`UNKNOWN: "Internal Server Error"` to the client. `/status` showed `scheduler_node: null`.

**Evidence (`artifacts/at-d25bc86b/host0-brigade.log`):**
```
[error] ** (exit) exited in: GenServer.call({:via, Horde.Registry, {Brigade.Scheduler.Registry, :singleton}},
        {:reserve, %{constraints: %{}, vcpu: 1, memory_mb: 1024, provider: nil}}, 5000)
    ** (EXIT) no process: the process is not alive or there's no process currently associated
       with the given name, possibly because its application isn't started
/status: ... "scheduler_node": null
```

Which node wins the singleton election is nondeterministic across runs (it correlates with which
node finishes bootstrapping first). Stopping the singleton's node (e.g. the quorum test stopping
node B) did not reliably migrate it to the surviving node.

**Fix direction:** ensure the Horde singleton reliably (re)starts on a surviving node and that
`scheduler_node` is never `null` while a quorum exists; surface a clean gRPC status (e.g.
`UNAVAILABLE`) instead of a raw `UNKNOWN`/`:noproc` when the scheduler is momentarily absent.

---

## Suite tests blocked by the above

| Test | Blocked by | Note |
|------|------------|------|
| `test_placement.py::test_placement_spreads_across_hosts` | Bug 1 / #13 (host aggregation, M3) | Marked `xfail(strict=True)` — flips to a hard failure the day brigade spreads. |
| `test_lifecycle.py::test_microvm_lifecycle` | Bug 1 / #13 (VM record visibility) + Bug 2 / #14 | Passes only when the singleton lands on the dialed node. |
| `test_quorum.py::test_quorum_gates_placement` | Bug 2 / #14 (singleton `:noproc` → `UNKNOWN`) | Quorum *gating* logic is implemented; the `UNKNOWN` failure is the singleton dying, not the gate. |
| `test_ssh_reachability.py::test_microvms_reachable_by_ssh` | Bug 1 / #13 + Bug 2 / #14 | Cascades from the same instability. |

## Suite-side fixes already applied (correct regardless of the above)

- `test_placement` — `xfail(strict=True)` with an M3 reason + `try/finally` cleanup that waits out
  deletes so it can't orphan VMs and starve later tests.
- `test_quorum` — expect the refusal as `_TransientSchedulerError` (the client wraps transient
  scheduler codes) via `retry_timeout=0`, instead of a raw `grpc.RpcError` the retry can never
  surface.
- `logs.py` — added `sut-versions`, `sudo-audit`, `pid1-timeline`, `oom-memory`,
  `brigade-restart-stats` diagnostics.
