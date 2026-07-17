"""paramiko SSH client wrapper: connect-with-retry + command exec.

Used to drive host bootstrap over SSH. ``connect`` retries until the droplet's
sshd accepts the key (cloud-init lateness is the #1 flake), then ``run`` executes
commands and returns (exit_status, stdout, stderr).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import paramiko

from ..waiter import retry_call

log = logging.getLogger("ssh")


class CommandError(RuntimeError):
    def __init__(self, cmd: str, rc: int, out: str, err: str):
        # Show BOTH streams: provision scripts echo progress/failures to stdout,
        # while apt/debconf noise lands on stderr. Picking one stream hides the
        # real failure whenever the other stream is non-empty.
        parts = [f"command failed (rc={rc}): {cmd}"]
        if out.strip():
            parts.append(f"--- stdout ---\n{out.strip()}")
        if err.strip():
            parts.append(f"--- stderr ---\n{err.strip()}")
        super().__init__("\n".join(parts))
        self.cmd, self.rc, self.out, self.err = cmd, rc, out, err


@dataclass
class SSH:
    host: str
    user: str
    key_path: str
    port: int = 22
    _client: paramiko.SSHClient | None = None
    # optional tunnel channel (for bastion/ProxyJump connections)
    _sock: object | None = None

    def _new_client(self) -> paramiko.SSHClient:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=self.key_path,
            sock=self._sock,  # type: ignore[arg-type]
            timeout=15,
            banner_timeout=30,
            auth_timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        return c

    def connect(self, *, timeout: float) -> SSH:
        """Retry connect until sshd accepts our key or ``timeout`` elapses."""
        self._client = retry_call(
            self._new_client,
            timeout=timeout,
            exceptions=(OSError, paramiko.SSHException),
            description=f"ssh {self.user}@{self.host}:{self.port}",
        )
        return self

    @property
    def client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise RuntimeError("connect() not called")
        return self._client

    def run(self, cmd: str, *, check: bool = True, timeout: float = 600) -> tuple[int, str, str]:
        log.info("[%s] $ %s", self.host, cmd if len(cmd) < 120 else cmd[:117] + "...")
        _, stdout, stderr = self.client.exec_command(cmd, timeout=timeout, get_pty=False)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if check and rc != 0:
            raise CommandError(cmd, rc, out, err)
        return rc, out, err

    def sudo(self, cmd: str, **kw) -> tuple[int, str, str]:
        return self.run(f"sudo bash -c {_shquote(cmd)}", **kw)

    def put(self, content: str, remote_path: str) -> None:
        """Write ``content`` to ``remote_path`` via SFTP."""
        sftp = self.client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def open_direct_channel(self, dest_host: str, dest_port: int) -> object:
        """Open a direct-tcpip channel through this connection (bastion hop)."""
        transport = self.client.get_transport()
        assert transport is not None
        return transport.open_channel(
            "direct-tcpip", (dest_host, dest_port), (self.host, 0)
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
