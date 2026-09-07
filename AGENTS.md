# AGENTS.md

Guide for coding agents working in this repo. See `README.md` for the full human-facing docs.

## Overview

End-to-end acceptance suite for **flintlock** microVM orchestration, running on real
**DigitalOcean** infrastructure. Two independent suites:

- **brigade** (`tests/`) — a run provisions a VPC + SSH key + 2 droplets (2 flintlock hosts) +
  block volumes + firewall, bootstraps containerd/Firecracker/`flintlockd` and a 2-node
  brigade cluster, drives the flintlock gRPC API through brigade, then tears everything down.
- **battery** (`tests/battery/`) — a single-instance MicroVM warm-pool manager (`poolmgrd`,
  Go, pre-alpha) that dials N flintlockd hosts directly and exposes its own gRPC API
  (`PoolAdmin`/`Lease`/`Events`). Self-contained (own `conftest.py`), reuses the same DO
  provisioning + flintlockd bootstrap. See `docs/battery-known-gaps.md` for constraints found
  while building it (pool `size` must stay `1`; no per-VM placement info in its API).

## Setup

```bash
make venv                 # create .venv, install runtime + dev deps
make proto                # generate gRPC stubs from vendored protos
cp .env.example .env      # then fill DO_API_TOKEN + MICROVM_*_IMAGE + SSH key paths
```

`DO_API_TOKEN` needs **Full Access** (or a custom-scopes token with create/read/delete on
droplet, block_storage, block_storage_action, vpc, firewall, ssh_key, tag). A read-only
token fails on first write. `block_storage_action` is easy to miss — attaching a volume to a
droplet is a *volume action*, gated separately from `block_storage` (volume create/delete).

## Commands

```bash
.venv/bin/pytest tests/test_cleanup.py   # offline unit checks — no DO token, no infra, fast
make test                                # brigade e2e: real infra, ~20-40 min, costs money
make test-battery                        # battery e2e: real infra, separate run, costs money
make lint                                # ruff check
make proto                               # regenerate gRPC stubs (flintlock + battery)
make refresh-proto                       # re-fetch + revendor upstream flintlock protos
make refresh-battery-proto               # re-fetch + revendor battery's own protos
make clean-tags                          # reap leftover at-* / lm-acceptance-* DO resources
```

Config is entirely env-driven — see `.env.example` for every knob. `RUN_ID` (auto `at-<hex>`)
tags and isolates a run. Set `KEEP_INFRA_ON_FAILURE=true` to leave infra up for debugging;
host `journalctl` is always collected to `artifacts/<run_id>/`.

## Layout

```
liquidmetal_at/
  config.py            env → Config, RUN_ID, validation
  infra/               DigitalOcean provisioning (do.py) + teardown (reaper.py)
  bootstrap/           host + brigade + battery bootstrap, Jinja2 templates/
  flintlock/           gRPC client + generated stubs (gen/, shared with battery/)
  battery/             gRPC client to poolmgrd (PoolAdmin/Lease/Events) + PoolSpec builder
tests/                 brigade pytest suite (test_cleanup.py offline; e2e marked)
tests/battery/         battery pytest suite (own conftest.py, e2e marked)
proto/                 vendored, stripped flintlock + battery protos
scripts/               refresh_protos.py, refresh_battery_protos.py
```

## Conventions

- **ruff** — line-length 100, target py310, rules `E,F,I,W,UP,B`. Run `make lint` before finishing.
- **Do not edit or lint generated stubs** under `liquidmetal_at/flintlock/gen/**` — regenerate via `make proto`.
- Env-driven config only; never hardcode tokens/paths — read from `.env` / `Config`.

### Upstream-sensitive spots

These encode best-known upstream behaviour and are the most likely to need version tweaks:

- `bootstrap/templates/provision_host.sh.j2` — `provision.sh` subcommands + `flintlockd run` args.
- `bootstrap/templates/brigade_config.exs.j2` — brigade `config.exs` schema.
- `brigade_status.py` — the `/status` JSON schema (parsing is intentionally tolerant).
- `bootstrap/templates/provision_battery.sh.j2` — the `poolmgrd` release asset naming.
- `bootstrap/templates/battery_config.json.j2` — poolmgrd's JSON config schema.
- `proto/poolmgr/v1alpha1/*.proto` — battery's own gRPC surface; battery is pre-alpha, so this
  is the most likely spot to need a re-vendor when bumping `BATTERY_REF`.

## Git rules — IMPORTANT

- **Do NOT add `Co-Authored-By` trailers to commits.**
- **Do NOT add any "Generated with Claude Code", "Authored-By", or other agent-attribution
  lines to commit messages or PR bodies.** Commits and PRs must read as authored by the human.
- Commit or push only when explicitly asked. If on `main`, branch first.
- Use the `gh` CLI for GitHub operations.
