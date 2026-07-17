"""Collect host logs to the artifacts dir for post-mortem debugging."""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .infra.do import EPMD_PORT, ERLANG_DIST_LOW, Droplet, Infra
from .remote.ssh import SSH

log = logging.getLogger("logs")

# systemd units whose journals we always pull.
_UNITS = ("flintlockd", "brigade", "containerd")


def _diag_commands(cfg: Config, peers: list[Droplet]) -> list[tuple[str, str]]:
    """Read-only probes that reveal why the brigade Erlang mesh did or didn't form."""
    cmds = [
        ("listeners", "ss -tlnp"),
        ("epmd-names", "epmd -names"),
        (
            "services",
            "systemctl status brigade flintlockd containerd --no-pager -l | tail -80",
        ),
        ("brigade-unit", "cat /etc/systemd/system/brigade.service"),
        ("brigade-config", "cat /opt/brigade/config/config.exs"),
        ("status", f"curl -s localhost:{cfg.brigade_status_port}/status"),
        # Exact SUT versions under test. brigade/flintlock main are re-cloned per run, so
        # they drift — pinning the commit that failed is essential for upstream bug reports
        # (e.g. the node-local Mnesia / unstable scheduler-singleton issues).
        (
            "sut-versions",
            "echo -n 'brigade='; git -C /opt/brigade rev-parse --short HEAD 2>/dev/null; "
            "git -C /opt/brigade log -1 --format='  %ci %s' 2>/dev/null; "
            "echo -n 'flintlock='; git -C /opt/flintlock rev-parse --short HEAD 2>/dev/null; "
            "git -C /opt/flintlock log -1 --format='  %ci %s' 2>/dev/null; "
            "flintlockd version 2>/dev/null | head -1 || true",
        ),
        # Why does systemd restart brigade mid-run? PID-1's own log + reverse deps reveal
        # the trigger (dependency stop, conflict, manual restart, OOM).
        (
            "brigade-restart-why",
            "journalctl -b _PID=1 --no-pager | grep -iE 'brigade|flintlock' | tail -50",
        ),
        # A graceful SIGTERM to brigade ("Stopping brigade orchestrator") is a *deliberate*
        # stop/restart, not a crash. These pin the trigger. sudo-audit shows every sudo
        # command on the box, so an SSH-issued `systemctl stop brigade` (e.g. the quorum
        # test hitting the wrong node, or unexpected ordering) is unmissable here; its
        # absence proves the stop came from inside systemd, not the suite.
        (
            "sudo-audit",
            "journalctl _COMM=sudo -b --no-pager | tail -40",
        ),
        # Full PID-1 job context around the event — the line *before* "Stopping brigade"
        # names the unit/target/isolate that triggered it (a plain restart shows
        # Stopped→Started with no conflicting unit).
        (
            "pid1-timeline",
            "journalctl _PID=1 -b -o short-precise --no-pager | grep -iE "
            "'brigade|flintlock|stopping|stopped|started|isolat|conflict|reload|"
            "scheduled restart|shutdown|reboot' | tail -70",
        ),
        # OOM / memory-pressure kills: systemd-oomd sends a *graceful* SIGTERM to a cgroup
        # under pressure, which would look exactly like this. Kernel OOM, oomd, live mem.
        (
            "oom-memory",
            "journalctl -b -k --no-pager | grep -iE 'oom|out of memory|killed process'"
            " | tail -20; echo '--- systemd-oomd ---'; "
            "journalctl -u systemd-oomd -b --no-pager | tail -20; "
            "echo '--- free ---'; free -m",
        ),
        # brigade unit restart accounting: how many times, why it last exited, and the
        # active/inactive transition timestamps to correlate with the test timeline.
        (
            "brigade-restart-stats",
            "systemctl show brigade.service -p NRestarts -p Result -p ExecMainStatus "
            "-p ActiveEnterTimestamp -p InactiveEnterTimestamp -p InactiveExitTimestamp",
        ),
        (
            "brigade-deps",
            "systemctl show brigade.service "
            "-p Requires -p Requisite -p BoundBy -p PartOf -p Conflicts "
            "-p TriggeredBy -p ConsistsOf -p NRestarts; "
            "systemctl list-dependencies --reverse brigade.service --no-pager",
        ),
        # microVM networking: TAP guests attach to the flintlock bridge; show its state.
        ("net-links", "ip -br link"),
        ("net-addrs", "ip -br addr"),
        ("bridge", "ip link show flintlock0; bridge link show 2>/dev/null"),
        ("nat", "iptables -t nat -S; sysctl net.ipv4.ip_forward"),
        # microVM boot: why a guest never reaches CREATED. KVM presence, the flintlock
        # per-VM state tree, and the running firecracker processes + their cmdlines.
        ("kvm", "ls -l /dev/kvm 2>&1; kvm-ok 2>&1 | tail -3"),
        ("flintlock-tree", "find /var/lib/flintlock -maxdepth 6 -type f 2>/dev/null"),
        (
            "firecracker-ps",
            "ps -eo pid,etimes,args | grep -i [f]irecracker; "
            "for p in $(pgrep -x firecracker); do echo \"cmdline $p:\"; "
            "tr '\\0' ' ' </proc/$p/cmdline; echo; done",
        ),
        ("cloud-init", "tail -200 /var/log/cloud-init-output.log"),
    ]
    # Cross-peer reachability: the direct signal for a broken mesh. Query each peer's EPMD
    # and one dist-range port from this host.
    for p in peers:
        ip = p.private_ip
        cmds.append(
            (
                f"peer-{ip}-epmd",
                f'timeout 5 bash -c "</dev/tcp/{ip}/{EPMD_PORT}" '
                f'&& echo OK || echo FAIL',
            )
        )
        cmds.append(
            (
                f"peer-{ip}-dist",
                f'timeout 5 bash -c "</dev/tcp/{ip}/{ERLANG_DIST_LOW}" '
                f'&& echo OK || echo FAIL',
            )
        )
    return cmds


def collect(cfg: Config, infra: Infra) -> None:
    out = Path(cfg.artifacts_dir) / cfg.run_id
    out.mkdir(parents=True, exist_ok=True)
    for i, d in enumerate(infra.droplets):
        try:
            ssh = SSH(host=d.public_ip, user="root", key_path=cfg.ssh_private_key_path)
            ssh.connect(timeout=30)
            for unit in _UNITS:
                _, journal, _ = ssh.run(
                    f"journalctl -u {unit} --no-pager -n 500 || true", check=False
                )
                (out / f"host{i}-{unit}.log").write_text(journal)

            peers = [p for p in infra.droplets if p.private_ip != d.private_ip]
            blocks = []
            for name, cmd in _diag_commands(cfg, peers):
                _, stdout, stderr = ssh.run(f"{cmd} || true", check=False)
                blocks.append(f"== {name} ==\n$ {cmd}\n{stdout}{stderr}")
            (out / f"host{i}-diag.txt").write_text("\n\n".join(blocks) + "\n")

            # Per-VM firecracker logs (debug-level guest boot output) — the evidence for
            # why a microvm won't reach CREATED. Written under /var/lib/flintlock/vm/**.
            _, fclogs, _ = ssh.run(
                "for f in $(find /var/lib/flintlock -name firecracker.log 2>/dev/null); do "
                'echo "== $f =="; cat "$f"; echo; done',
                check=False,
            )
            (out / f"host{i}-firecracker.log").write_text(fclogs)
            ssh.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not collect logs from host%d: %s", i, exc)
    log.info("collected host logs into %s", out)
