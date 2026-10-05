# battery known gaps (tests/battery/)

Notes from building the battery acceptance suite (https://github.com/liquidmetal-dev/battery,
pre-alpha, last updated for `v0.4.0`). `tests/battery/` passes (10 of 10) on
`BATTERY_REF=v0.4.0` + `FLINTLOCK_REF=v0.16.0`, run on the libvirt backend with Firecracker
and `ghcr.io/liquidmetal-dev/ubuntu:24.04`.

Two constraints are still open. The rest of this file records gaps that are now fixed,
kept short so the version floors and the reasons behind them stay findable. Two flintlock
issues found along the way are also still open; they are listed under the guest MAC entry.

## Open

### A pool can hold only one VM per host

`Provisioner.Provision` (`internal/reconciler/provision.go`) clones `pool.GetMicrovmTemplate()`
and overrides only `AllowGuestAgent`, `Id` (`<pool-name>-<8 hex>` per VM) and, when empty,
`Namespace`. An interface's static address and `guest_mac` are sent unchanged for every VM, so
since v0.4.0 (https://github.com/liquidmetal-dev/battery/issues/114) `CreatePool`/`UpdatePool`
return `INVALID_ARGUMENT` for a template that sets either when `size > 1`, when the strategy is
`IMMEDIATE_ON_LEASE`, or when `hook_failure_policy` is `QUARANTINE`.
`tests/battery/test_pool_validation.py` checks the rejection.

The suite therefore has two templates (`build_pool_spec(..., network=...)`):

- `static` (the default, shared with the brigade suite): a static IP and `guest_mac` per VM
  `index`. Only for `size: 1` pools.
- `dhcp`: no address and no `guest_mac`. Each battery host serves DHCP on its flintlock bridge
  (dnsmasq, set up by `bootstrap/host.py`) from its own range
  (`Config.microvm_dhcp_range`). `tests/battery/test_pool_multi_vm.py` uses it for a size-2
  pool and checks each VM's address by vsock, the lease file and SSH.

What is still limited with the `dhcp` template:

- **One pool VM per host.** A guest with no `guest_mac` derives its own MAC, and guests booted
  from one template derive the same one (`fa:90:93:1a:a6:c1` on both hosts in our runs). Two
  pool VMs on one bridge would share a MAC and a lease. The test sizes its pool to
  `NODE_COUNT`, and battery's host picker (fewest VMs for the pool first) then puts one on
  each host. Lifting this needs flintlock to give a TAP interface a unique guest MAC when
  none is set: https://github.com/liquidmetal-dev/flintlock/issues/1284.
- **Firecracker only.** Without `guest_mac` flintlock's netplan matches the NIC by name
  (`eth1`), which a Cloud Hypervisor guest does not use. `build_spec(network="dhcp")` raises
  for that provider and the test skips. https://github.com/liquidmetal-dev/flintlock/issues/1285.
- `IMMEDIATE_ON_LEASE` and `QUARANTINE` pools are not tested.

### No per-VM/host attribution in the API

`Pool` (`GetPool`/`ListPools`) exposes `PoolSpec` + `PoolStatus` (aggregate counts only). No RPC
returns a pool's individual VM records (host, uid, phase). `ClaimVM`'s response gives one VM's
`host` once claimed, `Lease.ListLeases` lists live leases (lease id, VM uid, timestamps) without
a host, and v0.4.0's `HostAdmin.ListHosts` gives a `vm_count` per host but not which VMs
(`tests/battery/test_host_cordon.py` uses `ClaimVM`'s `host` to check a cordon). `ClaimVM` returns no guest address either, so
`liquidmetal_at/battery/guest.py::assert_claimed_vm_accessible` asks the guest for it over
vsock and then SSHes in; `test_claim_release` and `test_pool_multi_vm` use it to check that a
claimed VM, and its replacement, can actually be used. The other tests check API behaviour
only. Any other
placement-style assertion has to fall back to the SSH ground-truth technique
`tests/test_placement.py` uses against each flintlock host's `/var/lib/flintlock/vm/` state.

## Resolved

### Guest MAC collided with flintlock's metadata interface (this repo)

`build_spec` used to generate `aa:ff:00:00:<index>` as the guest MAC. flintlock gives every VM's
metadata interface (eth0) the fixed MAC `AA:FF:00:00:00:01`, so the VM at index 1 had the same
MAC on two NICs. Firecracker rejects that config and exits ("The MAC address is already in use"),
flintlock still reports the VM `CREATED`, and the only symptom from battery's side is
`dial unix /run/flintlock/<uid>/guest-agent.vsock: connect: no such file or directory` until
`WaitReady` gives up. `test_events` (index 1) failed this way on every attempt. The scheme is
now `aa:ff:00:01:<index>`. When a VM never becomes ready, read
`/var/lib/flintlock/vm/<ns>/<name>/<uid>/firecracker.log` on the host before battery deletes it.

Both flintlock behaviours that hid this are still open upstream:

- https://github.com/liquidmetal-dev/flintlock/issues/1283: `CreateMicroVM` accepts a
  `guest_mac` equal to the metadata interface's MAC instead of rejecting it.
- https://github.com/liquidmetal-dev/flintlock/issues/1263: a MicroVM is reported `CREATED`
  when its VMM has already exited.

### `DeletePool` refused any pool that still had VMs (fixed in v0.4.0)

Before v0.4.0 `DeletePool` returned `FAILED_PRECONDITION` for a pool with any VM, and the API
had no way to empty one. Since v0.4.0 it deletes the pool's unleased VMs itself and refuses only
while a VM is leased, unless `DeletePoolRequest.force` is set.
https://github.com/liquidmetal-dev/battery/issues/112. `tests/battery/test_delete_pool.py`
covers the leased case, and the `finally` cleanups in the other tests pass `force=True`.

### A failed provision in an event-driven pool was never retried (fixed in v0.4.0)

Before v0.4.0 `REPLACE_ON_DELETE` and `IMMEDIATE_ON_LEASE` pools provisioned only on the
start-of-life seed (itself added in v0.3.2, `327efbb`) and on delete/claim notifications, so one
failed provision left a `size: 1` pool empty for good. Since v0.4.0 the reconciler tops these
pools back up to `size` on every tick. https://github.com/liquidmetal-dev/battery/issues/113.

### Guest-agent socket path overflowed `sun_path` (fixed in flintlock v0.15.2)

battery v0.3.1+ names each VM `<pool-name>-<8 hex>`. Before flintlock v0.15.2 that id and the
namespace were part of the guest-agent socket path, which overflowed Linux's 107-byte
`sun_path` and failed every VM with `connect: invalid argument`
(https://github.com/liquidmetal-dev/battery/issues/94, flintlock#1226, fixed by flintlock#1227).
Sockets now live at `/run/flintlock/<uid>/`. `tests/battery/conftest.py` stops the run up front
if `FLINTLOCK_REF` is a release tag older than v0.15.2, and battery v0.3.3+ checks the same.

### flintlock exec-API hangs and handshake failures (fixed by flintlock v0.15.1)

battery's readiness probe (`flintlockclient.WaitReady`, a no-op exec over flintlock's
`MicroVMExec`) never completed on flintlock v0.14.0, which blocked every pool:

- https://github.com/liquidmetal-dev/flintlock/issues/1200: the exec session read had no
  deadline and hung forever. v0.14.1 bounded it with an idle deadline (#1201); v0.15.0 replaced
  that with guest-agent heartbeats (#1203/#1204, guest-agent v0.4.0).
- https://github.com/liquidmetal-dev/flintlock/issues/1205: the vsock `CONNECT` handshake got
  EOF after repeated dials. Fixed in v0.15.1 with a bounded retry.

On v0.16.0 VMs become ready 20 to 30 seconds after flintlock reports `CREATED`. battery's
`WaitReady` budget is 30 seconds, which is tight on nested virtualization; with the retry from
battery#113 a miss now costs one more provision instead of the pool.

### battery runbook said lease expiry was blocked (fixed in v0.4.0)

`docs/runbooks/e2e-manual-verification.md` in the battery repo claimed `poolmgrd` never ran the
`Sweeper`. It has since v0.1.0, and `tests/battery/test_lease_expiry.py` exercises it.
https://github.com/liquidmetal-dev/battery/issues/115.
