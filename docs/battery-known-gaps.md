# battery known gaps (tests/battery/)

Notes from building the battery acceptance suite (https://github.com/liquidmetal-dev/battery,
pre-alpha at `v0.1.0`) against this repo's real-infra pattern. Filed here rather than upstream
issues since these are usage constraints confirmed by reading the source, not yet reported bugs.

## `PoolSpec.microvm_template` is applied verbatim to every VM in the pool

Confirmed by reading `internal/reconciler/provision.go`: `Provision` clones
`pool.GetMicrovmTemplate()` with `proto.Clone` and only overrides `AllowGuestAgent` server-side
before calling `CreateMicroVM`. Nothing else — `id`, the interface's static IP, and
`guest_mac` are all reused byte-for-byte for every VM the reconciler provisions for that pool.

Our `microvm_template` (`liquidmetal_at/flintlock/spec.py::build_spec`, reused from the brigade
suite) sets a static IP per VM `index` — fine for a single flintlock create, but unsafe for a
battery pool with `size > 1`: every VM in the pool would get the identical static address and
`id`. `tests/battery/` works around this by keeping every pool's `size` at `1`. If a future
scenario needs `size > 1`, either use a template with a DHCP/MMDS-based address instead of a
static one, or wait for upstream to support per-VM template variation.

## No per-VM/host attribution in the `PoolAdmin` API

`Pool` (`GetPool`/`ListPools`) only exposes `PoolSpec` + `PoolStatus` (aggregate counts:
`available_count`/`leased_count`/`provisioning_count`/`quarantined_count`) — there's no RPC that
returns the pool's individual `VMRecord`s (which host each VM landed on, its uid, its phase).
`ClaimVM`'s response does return one VM's `host` once it's claimed, but there's no way to inspect
placement across a pool's VMs without claiming all of them. Any placement-style assertion has to
fall back to the same SSH ground-truth technique `tests/test_placement.py` already uses against
each flintlock host's own `/var/lib/flintlock/vm/` state, rather than the battery API.

## Runbook doc lags `main`

`docs/runbooks/e2e-manual-verification.md` (in the battery repo) states `cmd/poolmgrd/main.go`
"never constructs or runs `reconciler.Sweeper`" and marks lease expiry (step 9) blocked. Reading
`main.go` directly at `v0.1.0` shows this is stale: `reconciler.NewSweeper(...)` is constructed
and run alongside the API/metrics servers. `tests/battery/test_lease_expiry.py` exercises it.
Re-check this doc's accuracy whenever bumping `BATTERY_REF`.
