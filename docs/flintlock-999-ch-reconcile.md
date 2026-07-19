# flintlock #999 — Cloud Hypervisor reconcile churn & socket orphaning

Upstream issue: <https://github.com/liquidmetal-dev/flintlock/issues/999>
Observed on: flintlock `main` @ `729de8a` (also the released `flintlockd v0.9.0` binary),
Cloud Hypervisor v53.0, nested KVM on DigitalOcean droplets.

This is a write-up from the Liquid Metal acceptance suite. It reproduces #999 deterministically
enough to root-cause it, and proposes the minimal fix. Nothing here is worked around in the
suite — a CH run is simply re-run when it trips the flake.

## Symptom

With `provider: cloudhypervisor`, VM creates intermittently (~1 run in 3, higher under load /
nested virt) never reach `CREATED`; `GetMicroVM` keeps reporting a non-created state until the
client times out. `cloudhypervisor.stderr` for affected (and unaffected) VMs fills with
hundreds of:

```
Error: Cloud Hypervisor exited with the following chain of errors:
  0: Failed to start the VMM thread
  1: API socket "…/cloudhypervisor.sock" is already in use by another running instance
```

The guest that *does* win the race boots fine (cloud-init runs, NIC comes up, SSH works) — so
this is purely a control-plane reconcile bug, not a guest/boot problem.

## Root cause

1. **`State()` misclassifies CH's `"Created"` as `Pending`.**
   `infrastructure/microvm/cloudhypervisor/provider.go` `State()` queries the CH API
   (`chClient.Info`) and switches on CH's VM state:

   ```go
   case cloudhypervisor.VMStateRunning:  // "Running"
       return ports.MicroVMStateRunning, nil
   case cloudhypervisor.VMStateCreated:  // "Created"
       return ports.MicroVMStatePending, nil   // <-- bug
   ```

   Cloud Hypervisor reports `"Created"` for the window between the API socket binding and the
   guest finishing boot (that window is long under nested virt). During it, flintlock thinks
   the VM is still `Pending`.

2. **The create step's idempotency guard is state-only, so it re-fires.**
   `core/steps/microvm/create.go` `ShouldDo()` returns `state == MicroVMStatePending`; `Do()`
   then calls `vmSvc.Create` again — spawning a **second** cloud-hypervisor on the same
   `--api-socket` → `Address already in use` / tap `Resource busy`. Reconcile re-queues on the
   error/update event, so this repeats rapidly (the ~10-min resync period is not the driver).

3. **`ensureState()` deletes a *live* process's socket → permanent orphan.**
   `infrastructure/microvm/cloudhypervisor/create.go` `ensureState()` (run at the top of every
   `Create`) unconditionally unlinks the socket if the file exists:

   ```go
   if sockExists, _ := afero.Exists(p.fs, vmState.SockPath()); sockExists {
       p.fs.Remove(vmState.SockPath())
   }
   ```

   When the second `Create` fires while the first, healthy cloud-hypervisor is still bound to
   that socket, this removes the socket out from under the running process. `State()` can no
   longer `Info()` it → returns `Unknown`/error forever → retries exhaust `MaximumRetry` → the
   VM is marked `FailedState` and never reaches `CREATED`. Most VMs win the race and converge;
   the unlucky one is stuck. That is the intermittent timeout.

Firecracker is unaffected: its `State()` treats "pidfile present + process alive" as `Running`,
so the create guard goes false as soon as the VM is up and never re-fires.

## Minimal fix

Two localized, ~5-line changes (either alone helps; together is defense-in-depth):

- **`provider.go` `State()`** — treat CH `"Created"` as `MicroVMStateRunning` (the process is up
  and the socket is bound; a VM mid-boot is, for flintlock's purposes, running). This stops the
  create step from ever re-firing.
- **`create.go` `ensureState()`** — before removing `SockPath()`, read the pidfile and skip the
  removal (and no-op the `Create`) when that PID is a live process. This prevents orphaning the
  running VM even if a stray re-create slips through.

Neither can terminate a running guest. This mirrors the fix proposed in the #999 comments,
which has not yet landed on `main`.

## Reproduction notes

- Provider selected via `MicroVMSpec.provider = "cloudhypervisor"`; `flintlockd` launched with
  `--cloudhypervisor-bin=/usr/local/bin/cloud-hypervisor`.
- Kernel: `ghcr.io/liquidmetal-dev/cloudhypervisor-kernel-k8s:6.2` (`vmlinux.bin`, PVH ELF);
  rootfs `ghcr.io/liquidmetal-dev/ubuntu:24.04`.
- Evidence per run in `artifacts/<run_id>/`: `host*-vm-hypervisor.log` (CH stderr with the
  socket errors + guest console on `hvc0`), `host*-flintlockd.log`, `host*-diag.txt`
  (`hypervisor-ps` shows multiple `cloud-hypervisor` processes for one VM id).
