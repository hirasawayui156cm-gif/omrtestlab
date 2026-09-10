"""SSH 连接封装：基于 paramiko，负责到路由器 / VPS 的远程命令执行。

- run():      一次性执行命令并等待返回（返回码、stdout、stderr）
- exec_stream(): 建立长连接命令，返回可逐行读取的流式句柄（用于实时监控 / 中途可中断的 iperf3）
"""
from __future__ import annotations

import threading
import time

import paramiko


class SSH:
    def __init__(self, host: str, port: int, user: str, password: str,
                 use_sudo: bool = False, timeout: float = 12.0):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.use_sudo = bool(use_sudo)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=host, port=int(port), username=user, password=password,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            allow_agent=False, look_for_keys=False, compress=True,
        )
        self._lock = threading.Lock()

    def _wrap(self, cmd: str) -> str:
        if self.use_sudo and cmd.strip() and not cmd.lstrip().startswith("sudo "):
            # sudo 用 secure_path（已含 /sbin、/usr/sbin），无需再补 PATH
            return "sudo -n " + cmd
        # 非 sudo 直连 shell 可能缺 sbin；顺序保证 /usr/bin(iputils ping) 优先于 /bin(busybox)
        return ("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH; "
                + cmd)

    def run(self, cmd: str, timeout: float = 90.0) -> tuple[int, str, str]:
        """一次性执行并等待返回。返回 (exit_code, stdout, stderr)。"""
        cmd = self._wrap(cmd)
        with self._lock:
            stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
        return rc, out, err

    def exec_stream(self, cmd: str) -> "SSHStream":
        """建立一条可流式读取、可中途关闭的长命令。"""
        cmd = self._wrap(cmd)
        with self._lock:
            stdin, stdout, stderr = self.client.exec_command(cmd, timeout=300)
        return SSHStream(stdin, stdout, stderr)

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


class SSHStream:
    """长命令句柄：逐行读取 stdout，可调用 stop() 中途关闭。"""

    def __init__(self, stdin, stdout, stderr):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._stop = False

    def readline(self, timeout: float = 30.0) -> str | None:
        """读取一行，超时返回 None；EOF 返回 ''。"""
        chan = self.stdout.channel
        while not self._stop:
            if chan.recv_ready():
                line = self.stdout.readline()
                if not line:
                    return ""
                return line.rstrip("\r\n")
            if chan.exit_status_ready() and not chan.recv_ready():
                rest = self.stdout.read()
                if rest:
                    return rest.decode("utf-8", "replace").rstrip("\r\n")
                return ""
            if not chan.get_transport().is_active():
                return ""
            chan.settimeout(0.2)
            time.sleep(0.05)
        return None

    def read_all(self, timeout: float = 90.0) -> str:
        """读取全部输出直至命令结束。"""
        chunks = []
        while True:
            line = self.readline(timeout)
            if line is None or line == "":
                break
            chunks.append(line)
        return "\n".join(chunks)

    def stop(self):
        """中途关闭通道（用于中止 iperf3 / 监控）。"""
        self._stop = True
        try:
            self.stdout.channel.close()
        except Exception:
            pass
