# battery known gaps (tests/battery/)

Notes from building the battery acceptance suite (https://github.com/liquidmetal-dev/battery,
pre-alpha, last updated for `v0.3.2`) against this repo's real-infra pattern. Filed here rather than upstream
issues since these are usage constraints confirmed by reading the source, not yet reported bugs.

## `PoolSpec.microvm_template`'s network config is applied verbatim to every VM in the pool

Confirmed by reading `internal/reconciler/provision.go`: `Provision` clones
`pool.GetMicrovmTemplate()` with `proto.Clone` and overrides `AllowGuestAgent`, `Namespace`
(only when empty) and — since v0.3.1 (`943c521`, "assign each provisioned VM its own id") —
`Id`, which it sets to `<pool-name>-<8 hex>` per VM. The interface's static IP and `guest_mac`
are still reused byte-for-byte for every VM the reconciler provisions for that pool.

Our `microvm_template` (`liquidmetal_at/flintlock/spec.py::build_spec`, reused from the brigade
suite) sets a static IP per VM `index` — fine for a single flintlock create, but unsafe for a
battery pool with `size > 1`: every VM in the pool would get the identical static address.
`tests/battery/` works around this by keeping every pool's `size` at `1`. If a future
scenario needs `size > 1`, either use a template with a DHCP/MMDS-based address instead of a
static one, or wait for upstream to support per-VM address variation.

## Pool name + namespace must fit the guest-agent vsock socket path (battery#94)

The v0.3.1 generated id becomes a directory in flintlock's guest-agent socket path,
`/var/lib/flintlock/vm/<namespace>/<pool-name>-xxxxxxxx/<26-char ULID>/guest-agent.vsock`, which
Linux caps at 107 usable bytes (`sun_path`). That leaves **30 bytes** for `namespace` + pool
name combined. Upstream doesn't validate this: `CreatePool` accepts an over-long pool, and every
VM then fails after `CreateMicroVM` succeeds, with `connect: invalid argument`.

This suite used to name pools `<run_id>-pool-<x>` in namespace `<run_id>` (`at-xxxxxxxx`),
which came to about 114 bytes — over the limit for every test. Pools are now named `pool-<x>`
(the namespace already isolates the run), and `battery/spec.py::build_pool_spec` raises
`ValueError` offline for anything over the limit, including a long `MICROVM_NAMESPACE`
override. Filed upstream: https://github.com/liquidmetal-dev/battery/issues/94.

## Event-driven pools were never seeded before v0.3.2

Before battery v0.3.2 (`327efbb`, "seed event-driven pools when their reconciler starts"), a
`REPLACE_ON_DELETE` or `IMMEDIATE_ON_LEASE` pool only provisioned in response to a delete or a
claim. A freshly created pool was empty, so nothing could be claimed or deleted, and it never
provisioned anything. 3 of the 4 tests here (`test_claim_release`, `test_events`,
`test_lease_expiry`) use `REPLACE_ON_DELETE`, so on v0.1.0 they would have timed out waiting for
`AVAILABLE` regardless of flintlock. This is likely a contributor to the failures below that
were attributed only to flintlock's exec API. v0.3.2 tops these pools up to `size` whenever
their reconciler starts (poolmgrd startup, `CreatePool`, `UpdatePool`).

## No per-VM/host attribution in the `PoolAdmin` API

`Pool` (`GetPool`/`ListPools`) only exposes `PoolSpec` + `PoolStatus` (aggregate counts:
`available_count`/`leased_count`/`provisioning_count`/`quarantined_count`) — there's no RPC that
returns the pool's individual `VMRecord`s (which host each VM landed on, its uid, its phase).
`ClaimVM`'s response does return one VM's `host` once it's claimed, but there's no way to inspect
placement across a pool's VMs without claiming all of them. `Lease.ListLeases` (v0.2.0) now
lists a pool's live leases (`LeaseRecord`: lease id, VM uid, timestamps), which the suite uses
to assert release/expiry directly, but it only covers leased VMs and still carries no host. Any placement-style assertion has to
fall back to the same SSH ground-truth technique `tests/test_placement.py` already uses against
each flintlock host's own `/var/lib/flintlock/vm/` state, rather than the battery API.

## Runbook doc lags `main`

`docs/runbooks/e2e-manual-verification.md` (in the battery repo) states `cmd/poolmgrd/main.go`
"never constructs or runs `reconciler.Sweeper`" and marks lease expiry (step 9) blocked. Reading
`main.go` directly (at `v0.1.0`, and still at `v0.3.2`, where the runbook is unchanged on this
point) shows this is stale: `reconciler.NewSweeper(...)` is constructed
and run alongside the API/metrics servers. `tests/battery/test_lease_expiry.py` exercises it.
Re-check this doc's accuracy whenever bumping `BATTERY_REF`.

## flintlock's exec-API session never completes (blocks every pool indefinitely)

As of `FLINTLOCK_REF=v0.14.0` (current tip of `main` at the time of writing — no newer commit
exists to pull in), **every `tests/battery/` test times out** waiting for a VM to reach
`AVAILABLE`, regardless of which rootfs/kernel image is used. This reproduces identically with
flintlock's official prebuilt `flintlockd` release binary, so it isn't specific to building from
source.

Root cause, traced end-to-end across three log sources for a single failing VM:

- `poolmgrd`'s reconciler (`internal/reconciler/provision.go`, see the gap above) calls
  `flintlockclient.WaitReady`, which issues a no-op `exec true` over flintlock's `MicroVMExec`
  gRPC service (`--enable-exec-api`, added in flintlock v0.13.0 — see `battery/spec.py`).
- flintlockd's handler (`infrastructure/grpc/exec_server.go::ExecCommand`) opens a vsock session
  to the guest-agent and loops on `session.Next()` until it sees an `EventExit`, at which point it
  returns. That read has **no deadline** (the code's own comment acknowledges this — a context
  cancellation is the only other way out).
- The raw vsock wire trace (Firecracker's own trace-level device log) shows the guest-agent
  completing the exchange cleanly: it answers the exec request, then half-closes and RSTs the
  connection. flintlockd's `session.Next()` never returns after that — no error, no `EventExit`,
  nothing. `poolmgrd`'s RPC (and therefore the whole reconcile loop for that pool) hangs forever;
  `poolmgrd`'s own log goes silent immediately after the one exec attempt (it only logs at
  ERROR/startup by default, so a hang produces no output at all), and flintlockd itself never logs
  again either since it's parked on that same blocked read.

In short: `infrastructure/vsockexec`'s session reader doesn't detect the guest side closing the
connection, and never returns a corresponding `EventExit`/error. Every battery test needs at least
one VM's `CREATE_HOOK_RUNNING` guest-agent check to complete, so this blocks the entire suite until
it's fixed upstream. No config or image change in this repo can work around it — filed here since
it's a bug in flintlock's exec/vsock plumbing itself, not this repo's usage of it.

Filed upstream: https://github.com/liquidmetal-dev/flintlock/issues/1200.

**Update (flintlock v0.14.1):** flintlock v0.14.1 ships `#1201` ("fix(exec): bound guest-agent
exec session reads with an idle deadline"), which for battery's exact call shape
(`flintlockclient.WaitReady` → `Exec` with `ExecOptions{}`, i.e. `TimeoutSec: 0`) falls back to a
15-minute `ExecSessionUnboundedIdleCeiling` (`pkg/defaults/defaults.go`) rather than leaving the
read fully unbounded. Confirmed this constant is present in the v0.14.1 tag. Re-ran
`tests/battery/` against v0.14.1 and it still fails the same way within our default
`TIMEOUT_POOL_AVAILABLE=300s` (5 min) — well short of the 15-minute ceiling, so this repo's own
timeout was too impatient to observe whether the reconciler actually recovers once that ceiling
fires. `TIMEOUT_POOL_AVAILABLE` has been bumped to `1200`s in `.env.example` to give it room to.

That said, upstream itself now doubts the idle-timeout/missing-EOF diagnosis is the real root
cause: see https://github.com/liquidmetal-dev/flintlock/issues/1203 (filed by the same author),
which proposes replacing the `TimeoutSec`-derived deadline with a guest-agent heartbeat signal
once available, and explicitly notes "#1201's review discussion also raised an open question on
whether the exact scenario in #1200 ... is actually explained by a missing-EOF/idle-timeout
theory at all." Per discussion with the repo owner, holding off on further real-infra e2e runs
against this until a real root-cause fix lands upstream (tracked by #1203, blocked on
liquidmetal-dev/guest-agent#13) rather than spending infra time confirming/refuting the
15-minute-ceiling workaround.

Thread, in order: https://github.com/liquidmetal-dev/flintlock/issues/1200 (original bug) →
https://github.com/liquidmetal-dev/flintlock/issues/1200#issuecomment-5581737376 (incorrect
"no idle deadline at all" claim — see next comment) →
https://github.com/liquidmetal-dev/flintlock/issues/1200#issuecomment-5581782906 (correction:
15-min ceiling does exist) → https://github.com/liquidmetal-dev/flintlock/issues/1203 (root
cause likely misdiagnosed; heartbeat-based redesign proposed).

**Update (flintlock v0.15.0):** #1203 landed as `#1204` ("feat(exec): use guest-agent heartbeats
for exec idle deadline", fixes #1203) and shipped in flintlock v0.15.0, alongside guest-agent
v0.4.0. Re-ran `tests/battery/` against `FLINTLOCK_REF=v0.15.0` +
`ghcr.io/liquidmetal-dev/ubuntu:24.04` (freshly pulled — no local image cache to go stale, each
run provisions new droplets and containerd pulls fresh on the host).

Good news: **the original infinite hang is gone.** Where v0.14.0/v0.14.1 left `flintlockd`
permanently silent with no error ever surfacing, v0.15.0's reconciler now actually terminates
with a clear error after retrying:

```
ERROR reconciler: provision failed pool=<pool> error="reconciler: create hook failed: guest-agent
not ready: flintlockclient: WaitReady <uid>: timed out, last error: flintlockclient: exec <uid>:
rpc error: code = Unknown desc = starting guest-agent exec session: dialling guest-agent control
channel: handshake read: EOF"
```

Bad news: all 4 tests still fail, on a **different, lower-level fault**. "handshake read: EOF" is
not a heartbeat/guest-agent protocol issue — it's Firecracker's own vsock-UDS `CONNECT <port>\n`
/ `OK` handshake (`liquidmetal-dev/guest-agent/pkg/vsockclient.handshake`, used by flintlockd's
host-side dial to Firecracker's vsock multiplexer, *before* any guest-agent-level exchange even
begins) getting EOF instead of an `OK` reply. This points at Firecracker's vsock muxer itself,
not at anything in guest-agent's heartbeat/exec protocol. Plausible cause: exhausting something
in the muxer's connection-accept path after many rapid CONNECT/RST cycles from repeated
`WaitReady` retries, though this run only captured one terminal error (pool-lifecycle) in 65
minutes across 4 tests — the other 3 pools' reconcilers appear to keep retrying without ever
logging a comparable terminal error within our `TIMEOUT_POOL_AVAILABLE=1200s` window, so this
may still be masking further hangs rather than a clean, fast failure across the board.

Filed upstream as a separate issue (distinct from #1200/#1201/#1204, which are fixed):
https://github.com/liquidmetal-dev/flintlock/issues/1205

**Update (flintlock v0.15.1):** `#1205` was fixed by a bounded dial+handshake retry
("fix(vsockexec): retry transient guest-agent handshake failures", fixes #1205). Re-ran
`tests/battery/` against `FLINTLOCK_REF=v0.15.1` — still all 4 tests fail, but this run's
`poolmgrd` log recorded **zero** terminal `ERROR reconciler` lines across all 4 tests (vs. one in
the v0.15.0 run), suggesting the handshake-EOF fix worked and no pool hit that specific failure
this time.

However, the same "one exec exchange completes cleanly, then flintlockd logs nothing else"
pattern from the original #1200 hang reappeared for the run's last VM — right at the tail end of
the run, close enough to the test's own timeout/teardown that it's ambiguous whether this
specific case actually hung again or the run simply ended before the outcome could be observed
either way. Not clear-cut enough to treat as a confirmed regression of #1200, but also not a
clean pass. Needs a longer, dedicated run (or per-VM state polling instead of relying on log
tails) to disambiguate.

Re-run `tests/battery/` once a `FLINTLOCK_REF` newer than v0.15.1 is available, or investigate
directly why this run's last pool never reported success or failure in time.

**Update (battery v0.3.2):** there are two battery-side reasons every pool could stall before
reaching `AVAILABLE`, independent of the flintlock history above. Pools weren't seeded before
v0.3.2, and on v0.3.1+ the long pool names overflowed the socket path. Both are covered in their
own sections above and fixed or worked around here. Neither has been re-run on real infra yet, so
the flintlock issues above may not be the whole story. Re-run `tests/battery/` on
`BATTERY_REF=v0.3.2` + `FLINTLOCK_REF=v0.15.1` before drawing further conclusions about
flintlock#1200.
