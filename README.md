# Liquid Metal acceptance tests

End-to-end acceptance suite for **flintlock** microVM orchestration via **brigade**,
running on real **DigitalOcean** infrastructure.

Each run:

1. Provisions a VPC, an SSH key, and **2 droplets** (2 flintlock hosts) + a raw block
   volume per host + a firewall — all tagged for cleanup.
2. Bootstraps each host: containerd + Firecracker + `flintlockd` (via flintlock's
   `provision.sh`), then builds and runs a **brigade** node. The two brigade nodes form a
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
proto/                 vendored, stripped flintlock protos (see proto/README.md)
tests/                 conftest fixtures + 5 scenarios
```

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
  touches (`infra/do.py`, `infra/reaper.py`). Block storage and droplet **`create`/`delete`**
  are the ones most often missed:

  ```
  droplet:create        droplet:read        droplet:delete
  block_storage:create  block_storage:read  block_storage:delete
  vpc:create            vpc:read            vpc:delete
  firewall:create       firewall:read       firewall:delete
  ssh_key:create        ssh_key:read
  tag:create            tag:read            tag:delete
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
