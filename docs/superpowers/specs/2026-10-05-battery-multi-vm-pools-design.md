# Multi-VM battery pools: design

## Context

Every pool in `tests/battery/` is `size: 1`, because the shared template
(`liquidmetal_at/flintlock/spec.py::build_spec`) sets a static IP and `guest_mac`, and battery
v0.4.0 rejects that for any pool that can hold more than one VM. So nothing tests a pool of
several VMs. battery's design doc says such pools must leave both fields unset and let the
guest use DHCP. No host in this repo serves DHCP to microVMs.

Agreed in the brainstorm:

- **Goal:** prove battery's pool behaviour at size > 1 *and* that each pool VM gets its own
  working address, verified by the test.
- **Providers:** Firecracker now. Cloud Hypervisor is a recorded follow-up (without
  `guest_mac`, flintlock matches the NIC by name `eth1`, which CH guests don't use).
- **Scenarios:** (1) a size-2 pool fills, each VM has a distinct, reachable address;
  (2) the two VMs land on different hosts. `IMMEDIATE_ON_LEASE` and `QUARANTINE` are out.
- **Addressing:** dnsmasq on each host's flintlock bridge, battery runs only.

## Design

### 1. DHCP on the flintlock bridge (battery runs only)

- `provision_host.sh.j2` gains an `enable_guest_dhcp` block, wired like the existing
  `enable_exec_api` flag (`bootstrap/host.py::_provision_flintlock`, set by
  `bootstrap_host_battery`). Brigade hosts are unchanged.
- The block installs dnsmasq and runs it bound to the bridge only: `port=0` (DHCP only, so it
  can't clash with systemd-resolved on :53), router = the bridge address, DNS option =
  `1.1.1.1,8.8.8.8` (the guest still has to apt-install the guest agent).
- Each host gets a disjoint range so addresses are unique across the run, not just per host:
  host *i* serves `.{100+50i}` to `.{149+50i}` of `MICROVM_SUBNET_CIDR`. Static-IP tests keep
  using `.10+`. `bootstrap_host_battery` already receives `node_index`. Range helper lives on
  `Config` next to `microvm_static_ip`.

### 2. A DHCP mode for the pool template

- `build_spec` gains `network: "static" | "dhcp"` (default `static`, so brigade and the
  existing battery tests are untouched). In `dhcp` mode the interface has no `address` and no
  `guest_mac`; flintlock's netplan then sets `dhcp4: true`, `dhcp-identifier: mac` and matches
  by name (confirmed in flintlock `infrastructure/microvm/shared/network.go`).
- `build_pool_spec` passes it through and drops the `index` requirement in DHCP mode.
- `dhcp` mode with `MICROVM_PROVIDER=cloudhypervisor` raises a clear error; the new tests skip
  on CH with the reason.

### 3. How the test learns a VM's address

`ClaimVM` returns the VM uid, its host and interface MACs, but no IP. For a TAP interface with
no `guest_mac`, flintlock's reported MAC may be empty, so the test does not rely on it. It
asks the guest instead, over the channel battery already depends on:

1. On the hosting node (from `ClaimVM.host`), run
   `vsock-connect exec --uds /run/flintlock/<uid>/guest-agent.vsock --port 1024 -- ip -4 -o addr show eth1`
   (same pattern as `tests/test_guest_agent_vsock.py`).
2. Assert the address is inside that host's DHCP range and appears in
   `/var/lib/misc/dnsmasq.leases` on that host.
3. SSH to it through the node with the existing `remote/bastion.py::ssh_to_microvm` and run
   `ping -c1 1.1.1.1`.

Pool VMs share one hostname (the template's cloud-init is cloned), so identity is checked by
address, not hostname.

### 4. Tests (new file `tests/battery/test_pool_multi_vm.py`, Firecracker, NODE_COUNT >= 2)

One pool, `size == NODE_COUNT` (2), `REPLACE_ON_DELETE`, DHCP template, both hosts:

- wait for `available_count == 2`; claim both;
- the two claims report different hosts (battery's `PickHost` picks the host with the fewest
  VMs for the pool, ties by order, so one per host is guaranteed);
- each VM's address passes the three checks above, and the two addresses differ;
- release one; its replacement lands on the same host and passes the same address checks;
  force-delete in `finally`.

### 5. Docs

`docs/battery-known-gaps.md`: the "static template limits a pool to one VM" entry becomes
"static template only; DHCP template covers size > 1 on Firecracker", with CH as the open
part. `.env.example` / README get the one new knob if any is exposed (none planned).

## Spike results (libvirt, Firecracker, flintlock v0.16.0, battery v0.4.0)

All three risks cleared:

1. A Firecracker guest with no `guest_mac` brought `eth1` up by DHCP, installed the guest
   agent from the apt repo, and a size-2 pool was `AVAILABLE` in 80 seconds.
2. `vsock-connect` 0.1.0 can exec against the guest agent the apt repo installs today.
3. dnsmasq on the bridge answered guests behind flintlock's TAP devices with no extra
   firewall rule. Each VM took an address from its host's range and could ping out.

Two findings changed the design:

- **Guests from one template derive the same MAC.** Both VMs reported
  `fa:90:93:1a:a6:c1` for `eth1`. They were on different hosts, so each got its own lease, but
  two pool VMs on one bridge would share a MAC and a lease. The test therefore sizes the pool
  to one VM per host (`size == NODE_COUNT`), which battery's host picker guarantees. More than
  one DHCP pool VM per host needs flintlock to give a TAP interface a unique guest MAC when
  none is set.
- **`ClaimVM`'s `mac_address` is the host-side TAP device's MAC**, not the guest's, so it
  cannot be used to find a lease. Asking the guest over vsock (section 3) is the right call.

## Out of scope

Cloud Hypervisor; `IMMEDIATE_ON_LEASE`; `QUARANTINE`; per-VM address allocation in battery.
More than one DHCP pool VM per host (blocked by the shared guest MAC above).
Follow-up issues filed, both flintlock: a unique guest MAC for a TAP interface with none set
(flintlock#1284), and name-independent NIC matching for CH guests (flintlock#1285).

## Verification

- Offline: `make lint`, `.venv/bin/pytest tests/test_cleanup.py tests/test_config.py`, plus new
  unit checks for the DHCP range helper and `build_spec(network="dhcp")`.
- End to end: `INFRA_BACKEND=libvirt make test-battery` (all existing 9 tests plus the new
  one), FLINTLOCK_REF=v0.16.0, BATTERY_REF=v0.4.0.
