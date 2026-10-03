# libvirt backend (local KVM)

`INFRA_BACKEND=libvirt` runs both suites against local KVM virtual machines instead of
DigitalOcean droplets. It is meant for a bare-metal self-hosted GitHub runner and for a
developer workstation. No DigitalOcean token is needed.

Each run creates, under system libvirt (`qemu:///system`):

- a NAT network `lm-acceptance-<run_id>` on the first free `10.210.N.0/24`;
- `NODE_COUNT` VMs `lm-acceptance-<run_id>-host<i>` (4 vCPU / 8 GB / 50 GB by default), each
  a qcow2 overlay on a shared Ubuntu 22.04 cloud image;
- one serial console log per VM.

The base image is downloaded once to `~/.cache/lm-acceptance/`, verified against a pinned
SHA256, and stored in the `lm-acceptance` storage pool
(`/var/lib/libvirt/images/lm-acceptance`). It is reused across runs and never deleted
automatically.

## Requirements

- Bare metal x86-64 with hardware virtualization. The flintlock hosts run Firecracker /
  Cloud Hypervisor microVMs *inside* the VMs, so nested virtualization must be on.
- For the default two nodes: about 8 cores, 16 GB RAM and 100 GB of free disk.
- Internet access from the VMs (packages, toolchains, OCI images).

## Setup

### Ubuntu LTS (runner)

```bash
sudo apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst
sudo usermod -aG libvirt,kvm "$USER"      # the user the runner service runs as; re-login
```

### Arch (workstation)

```bash
sudo pacman -S --needed qemu-base libvirt virt-install dnsmasq
sudo systemctl enable --now libvirtd.socket
sudo usermod -aG libvirt "$USER"          # then log out and back in
```

### Nested virtualization

```bash
cat /sys/module/kvm_intel/parameters/nested   # Intel: Y or 1
cat /sys/module/kvm_amd/parameters/nested     # AMD: 1
```

If it is off, enable it and reload the module (or reboot):

```bash
echo "options kvm_intel nested=1" | sudo tee /etc/modprobe.d/kvm-nested.conf   # or kvm_amd
```

### Host firewall

With **ufw** (or another default-deny firewall) active and libvirt on its nftables
firewall backend, the VMs get no DHCP lease and no DNS: libvirt's accept rules live in a
separate table, so the firewall still drops the packets. Provisioning then fails with
"got no DHCP lease". Switch libvirt to the iptables backend:

```bash
sudo sed -i 's/^#\?firewall_backend *=.*/firewall_backend = "iptables"/' /etc/libvirt/network.conf
sudo systemctl restart libvirtd
```

**Docker** sets the iptables `FORWARD` policy to `DROP`, which can break the VMs' outbound
access. Keep Docker off the runner.

## Running

```bash
INFRA_BACKEND=libvirt make test           # brigade suite
INFRA_BACKEND=libvirt make test-battery   # battery suite
```

Or set `INFRA_BACKEND=libvirt` in `.env`. The other knobs (`LIBVIRT_*`) are listed in
`.env.example`; `NODE_COUNT` applies to both backends.

In CI, dispatch the **e2e-libvirt** workflow and pick a suite. The runner must carry the
labels `self-hosted`, `linux`, `x64`, `kvm`.

## Debugging and cleanup

- Host journals and diagnostics are collected to `artifacts/<run_id>/` as with
  DigitalOcean, plus `host<i>-console.log`, each VM's serial console. The console is the
  only evidence when a VM never boots or never gets an address.
- `KEEP_INFRA_ON_FAILURE=true` leaves the VMs up after a failed run:
  `ssh -i <key> root@10.210.N.10`, `virsh -c qemu:///system list --all`.
- `make clean-libvirt` removes every `lm-acceptance-*` VM, network and overlay volume,
  including kept ones. Base images stay; remove one with
  `virsh -c qemu:///system vol-delete --pool lm-acceptance <name>`.

## Bumping the base image

Pick a dated release under <https://cloud-images.ubuntu.com/releases/jammy/>, take the
`ubuntu-22.04-server-cloudimg-amd64.img` line from its `SHA256SUMS`, and update the two
defaults in `liquidmetal_at/config.py` (or set `LIBVIRT_BASE_IMAGE_URL` and
`LIBVIRT_BASE_IMAGE_SHA256`). The new image becomes a new base volume; the old one stays
until deleted by hand.
