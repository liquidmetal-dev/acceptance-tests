# Liquid Metal acceptance tests

End-to-end acceptance suite for **flintlock** microVM orchestration via **brigade**,
running on real **DigitalOcean** infrastructure.

Each run:

1. Provisions a VPC, an SSH key, and **2 droplets** (2 flintlock hosts) + a raw block
   volume per host + a firewall — all tagged for cleanup.
2. Bootstraps each host: containerd + Firecracker + Cloud Hypervisor + `flintlockd` (via
   flintlock's `provision.sh`), then builds and runs a **brigade** node. The two brigade nodes form a
   distributed-Erlang cluster (`min_cluster_size=2`).
3. Drives the flintlock gRPC API **through brigade** (north edge `:9091`): create / get /
   list / delete microVMs, verify placement spreads across both hosts, verify quorum
   gating, and **SSH into running microVMs** (ProxyJump via the hosting droplet).
4. Tears down all DigitalOcean resources — even on failure.

> **Nested virtualization:** flintlock needs `/dev/kvm`. DigitalOcean Basic droplets expose
> nested virt (as of 2026) but performance is poor — timeouts are sized generously.

## Layout

```
liquidmetal_at/
  config.py            env → Config, RUN_ID, validation
  waiter.py            tenacity poll/retry helpers
  infra/do.py          pydo provisioning + tag-scoped destroy
  infra/reaper.py      reap orphaned lm-acceptance-* resources (make clean-tags)
  bootstrap/           cloud-init + host provisioning (flintlock, brigade) over SSH
  remote/ssh.py        paramiko exec
  remote/bastion.py    ProxyJump tunnel to non-routable microVM IPs
  flintlock/client.py  gRPC client (create/get/list/delete + waiters)
  flintlock/spec.py    MicroVMSpec builder (static IP, cloud-init ssh key)
  brigade_status.py    parse brigade /status (cluster size, placement map)
  battery/client.py    gRPC client to poolmgrd (PoolAdmin/Lease/Events + waiters)
  battery/spec.py      PoolSpec builder (reuses flintlock/spec.py for microvm_template)
  battery/metrics_status.py  tolerant scrape of poolmgrd's /metrics
proto/                 vendored, stripped flintlock + battery protos (see proto/README.md)
tests/                 conftest fixtures + 5 brigade scenarios
tests/battery/         self-contained battery suite (own conftest) — see below
```

## battery (tests/battery/)

A second, self-contained suite for **battery** (https://github.com/liquidmetal-dev/battery), a
MicroVM warm-pool manager for flintlock. Unlike brigade, battery does not replace flintlock's
API — it's a single-instance Go daemon (`poolmgrd`) that dials N flintlockd hosts directly and
exposes its own gRPC API (`PoolAdmin`/`Lease`/`Events`). Because the bootstrap and fixture chain
diverge enough from brigade's, it lives under `tests/battery/` with its own `conftest.py`
(`config`/`infra`/`hosts`/`poolmgrd_node`/`battery_client`) rather than sharing the root one.

```bash
cp .env.example .env      # same file — fill in the --- battery --- section too
set -a && source .env && set +a
make test-battery          # provisions flintlock hosts + poolmgrd, runs tests/battery/, tears down
```

`make test` (brigade) and `make test-battery` are independent — each provisions and tears down
its own infra. `.venv/bin/pytest tests` runs both suites back to back if you want the full,
more expensive run. See `docs/battery-known-gaps.md` for constraints found while building this
(pool `size` must stay `1` for now; no per-VM placement info in the API).

## Prerequisites

- Python 3.10+
- A DigitalOcean API token with the right scopes — see [DigitalOcean API token](#digitalocean-api-token)
- An SSH keypair (the public key is uploaded to DO and injected into droplets + microVMs)
- **microVM OCI images** (kernel + rootfs) that boot with sshd and honour cloud-init
  user-data — you supply these (see `.env.example`).

## Setup

```bash
make venv                 # create .venv, install runtime + dev deps
make proto                # generate gRPC stubs from vendored protos
cp .env.example .env      # then fill in DO_API_TOKEN + MICROVM_*_IMAGE + SSH key paths
```

### DigitalOcean API token

The suite authenticates to DigitalOcean **only** — set your Personal Access Token (PAT) as
`DO_API_TOKEN` in `.env`. Create it in the [DigitalOcean Control
Panel](https://cloud.digitalocean.com) → **API** → **Personal access tokens** → **Generate New
Token**.

> **`you are not authorised`?** You almost certainly generated a **Read Only** token. It can
> list resources but not create/delete them, so the run fails on the first write (create VPC /
> droplet). Use **Full Access**, or a **Custom Scopes** token with the write scopes below.

The token needs write access to the resources the suite provisions and reaps. Two options:

- **Full Access** — simplest; works out of the box.
- **Custom Scopes** (least privilege) — select exactly these, one group per resource the suite
  touches (`infra/do.py`, `infra/reaper.py`). The ones most often missed: droplet and block
  storage **`create`/`delete`**, and **`block_storage_action`** — attaching a volume to a
  droplet is a *volume action*, gated separately from `block_storage` (volume create/delete).
  Omit it and the run fails with `you are missing the required permission
  block_storage_action:create` right after the volume is created:

  ```
  droplet:create               droplet:read               droplet:delete
  block_storage:create         block_storage:read         block_storage:delete
  block_storage_action:create  block_storage_action:read  block_storage_action:delete
  vpc:create                   vpc:read                   vpc:delete
  firewall:create              firewall:read              firewall:delete
  ssh_key:create               ssh_key:read
  tag:create                   tag:read                   tag:delete
  ```

See DigitalOcean's [personal access token
docs](https://docs.digitalocean.com/reference/api/create-personal-access-token/) for details on
custom scopes.

## Run

```bash
# offline unit checks (no DO token, no infra) — fast CI signal
.venv/bin/pytest tests/test_cleanup.py

# full end-to-end (provisions real infra, ~20-40 min; costs DO money)
set -a && source .env && set +a
make test                 # or: .venv/bin/pytest -m e2e
```

Config is entirely env-driven — see `.env.example` for every knob (region, droplet size,
VM count/shape, timeouts, git refs). `RUN_ID` (auto `at-<hex>`) isolates and tags a run.

### Cleanup

Teardown is automatic. If a run is killed, reap leftovers:

```bash
make clean-tags           # deletes all lm-acceptance-* tagged DO resources
```

Set `KEEP_INFRA_ON_FAILURE=true` to leave infra up for debugging when a test fails; host
`journalctl` for `flintlockd` + `brigade` is always collected to `artifacts/<run_id>/`.

## Known integration points to verify against upstream

The suite encodes best-known upstream behaviour; these are the spots most likely to need a
tweak against a specific flintlock/brigade version, and are isolated for easy editing:

- **`bootstrap/templates/provision_host.sh.j2`** — exact `provision.sh` subcommands/flags
  and the `flintlockd run` arguments (parent iface `eth1`, thinpool disk `/dev/sda`).
- **`bootstrap/templates/brigade_config.exs.j2`** — brigade `config.exs` schema
  (`min_cluster_size`, `host` capacity, libcluster Epmd topology) and the `mix run` start.
- **`brigade_status.py`** — the `/status` JSON schema (cluster size + placement map). Parsing
  is tolerant; the placement test falls back to per-host flintlock state on disk.
- **microVM images** — supplied by you via `MICROVM_KERNEL_IMAGE` / `MICROVM_ROOTFS_IMAGE`.
- **`bootstrap/templates/provision_battery.sh.j2`** — the `poolmgrd` release asset name
  (`poolmgrd_<version>_linux_amd64.tar.gz`) and systemd unit.
- **`bootstrap/templates/battery_config.json.j2`** — poolmgrd's own JSON config schema
  (`hosts`/`api_server`/`metrics_addr`/`sweep_interval`/`warning_window`).
- **`proto/poolmgr/v1alpha1/*.proto`** — battery's own gRPC surface. battery is pre-alpha, so
  this is the most likely spot to need a re-vendor (`make refresh-battery-proto && make proto`)
  when bumping `BATTERY_REF`.

### Hypervisor provider

`MICROVM_PROVIDER` selects the flintlock VM provider for a run — `firecracker` (default) or
`cloudhypervisor`. Both binaries are installed on every host and `flintlockd` registers both,
so the per-VM `MicroVMSpec.provider` field chooses the hypervisor; switch providers by
re-running the suite with a different `MICROVM_PROVIDER`. Cloud Hypervisor needs a
PVH-capable kernel — set `MICROVM_CH_KERNEL_IMAGE` / `MICROVM_CH_KERNEL_FILENAME` to override
the kernel when `MICROVM_PROVIDER=cloudhypervisor` (otherwise the firecracker kernel is reused).

Getting the Cloud Hypervisor path to boot + network a guest required three things the
Firecracker path doesn't (all handled in code / `.env`, but worth knowing):

1. **CH binary name** — `provision.sh` installs the release asset as `cloud-hypervisor-static`;
   `provision_host.sh.j2` symlinks it to `/usr/local/bin/cloud-hypervisor` (what
   `--cloudhypervisor-bin` expects). Without it, `flintlockd` registers the provider but every
   create dies at exec time (`starting cloud hypervisor process: %!w(<nil>)`).
2. **Kernel path** — the `cloudhypervisor-kernel-*` images ship the kernel at `vmlinux.bin`
   (root of the image, a PVH ELF), not `boot/vmlinux` — set `MICROVM_CH_KERNEL_FILENAME=vmlinux.bin`.
3. **Guest cloud-init + NIC** (`flintlock/spec.py`) — CH delivers cloud-init only via the FAT32
   `cidata` NoCloud disk (no MMDS), so we set `ds=nocloud` on the CH kernel cmdline; and CH is
   PCI-based so the guest names its NIC `ens4` (not `eth1` like Firecracker's `pci=off` MMIO
   guest). flintlock's netplan matches the interface by *name* unless a MAC is set, so we set a
   per-VM `guest_mac`, which flips the netplan to `match: {macaddress}` — name-agnostic, binds
   on both providers.

### Known upstream issue — Cloud Hypervisor CREATED-timeout flake (flintlock #999)

With `MICROVM_PROVIDER=cloudhypervisor`, `test_placement` / `test_ssh_reachability` fail
intermittently (~1 run in 3) with `WaitTimeout: microvm ... -> CREATED`, even though the guest
that *does* come up boots and networks correctly. This is an **unfixed upstream flintlock bug**
([liquidmetal-dev/flintlock#999](https://github.com/liquidmetal-dev/flintlock/issues/999)), not
a suite bug:

- flintlock's CH `provider.State()` maps Cloud Hypervisor's own `"Created"` state (the window
  after the API socket is up but before the guest finishes booting) to
  `MicroVMStatePending`. So the reconcile loop's `createStep.ShouldDo()` stays true and it
  spawns *another* cloud-hypervisor on the same `--api-socket` → `Address already in use` /
  `Resource busy` (hundreds of these per host in `cloudhypervisor.stderr`).
- `create.go`'s `ensureState()` then removes the socket file **even when a live process holds
  it**, orphaning the running CH so its state can never be read again → the VM lands in
  `FailedState` and never reaches `CREATED` → our `wait_state(CREATED)` times out.

Minimal upstream fix (see `docs/flintlock-999-ch-reconcile.md`): treat CH `"Created"` as
`Running` in `State()`, and don't delete the socket of a live PID in `ensureState()`. Until
that lands, a green CH run may need a re-run; the Firecracker suite is unaffected. Per-VM guest
consoles are captured to `artifacts/<run_id>/host*-vmconsole-*.log` (and the teardown
`host*-vm-hypervisor.log`) so the flake is diagnosable from artifacts.
