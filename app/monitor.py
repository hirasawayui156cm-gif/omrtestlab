"""链路实时监控：在路由器上流式采样 /proc/net/dev 计算各接口吞吐，并测量各链路 RTT。"""
from __future__ import annotations

import re
import threading
import time

from .ssh import SSH, SSHStream

_IFACE_RE = re.compile(r"^\s*([^:]+):\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+")


class ThroughputMonitor(threading.Thread):
    """流式读取 /proc/net/dev，每约 0.5s 生成一个吞吐快照。

    on_sample(sample: dict) 回调，sample 形如:
        {"t": epoch_seconds, "rx": {iface: mbps, "total": mbps},
         "tx": {iface: mbps, "total": mbps}}
    """

    def __init__(self, ssh: SSH, ifaces: list[str], duration: float,
                 on_sample, stop_event: threading.Event | None = None):
        super().__init__(daemon=True)
        self.ssh = ssh
        self.ifaces = ifaces
        self.duration = duration
        self.on_sample = on_sample
        self.stop_event = stop_event or threading.Event()
        self._stream: SSHStream | None = None
        self.samples: list[dict] = []

    def run(self):
        n = int(self.duration) + 3
        cmd = ("i=0; while [ $i -lt %d ]; do "
               "echo T$(awk '{print $1}' /proc/uptime); cat /proc/net/dev; echo ___; "
               "sleep 1; i=$((i+1)); done" % n)
        try:
            self._stream = self.ssh.exec_stream(cmd)
        except Exception as exc:
            self.on_sample({"error": str(exc)})
            return

        prev: dict[str, tuple[int, int]] = {}
        prev_t: float | None = None
        cur: dict[str, tuple[int, int]] = {}
        t = 0.0
        while not self.stop_event.is_set():
            line = self._stream.readline(timeout=10)
            if line is None or line == "":
                break
            if line.startswith("T"):
                t = float(line[1:])
                cur = {}
            elif line == "___":
                if prev_t is not None and prev:
                    dt = max(0.1, t - prev_t)
                    rx_all, tx_all = 0.0, 0.0
                    rx_map, tx_map = {}, {}
                    for iface in self.ifaces:
                        if iface in prev and iface in cur:
                            rx_map[iface] = max(0.0, (cur[iface][0] - prev[iface][0]) * 8.0 / dt / 1_000_000.0)
                            tx_map[iface] = max(0.0, (cur[iface][1] - prev[iface][1]) * 8.0 / dt / 1_000_000.0)
                            rx_all += rx_map[iface]
                            tx_all += tx_map[iface]
                    sample = {"t": t, "rx": rx_map, "tx": tx_map,
                              "rx_total": round(rx_all, 3), "tx_total": round(tx_all, 3)}
                    self.samples.append(sample)
                    if self.on_sample:
                        self.on_sample(sample)
                prev, cur = cur, {}
                prev_t = t
            else:
                m = _IFACE_RE.match(line)
                if m:
                    cur[m.group(1)] = (int(m.group(2)), int(m.group(3)))

    def stop(self):
        self.stop_event.set()
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass

    def summary(self) -> dict:
        """汇总各接口的峰值/平均吞吐（Mbps）。"""
        agg_rx: dict[str, list[float]] = {}
        agg_tx: dict[str, list[float]] = {}
        for s in self.samples:
            for iface in self.ifaces:
                agg_rx.setdefault(iface, []).append(s["rx"].get(iface, 0))
                agg_tx.setdefault(iface, []).append(s["tx"].get(iface, 0))
        out = {}
        for iface in self.ifaces:
            rx = agg_rx.get(iface, [])
            tx = agg_tx.get(iface, [])
            out[iface] = {
                "rx_avg_mbps": round(sum(rx) / len(rx), 3) if rx else 0.0,
                "rx_max_mbps": round(max(rx), 3) if rx else 0.0,
                "tx_avg_mbps": round(sum(tx) / len(tx), 3) if tx else 0.0,
                "tx_max_mbps": round(max(tx), 3) if tx else 0.0,
            }
        return out


def _parse_rtt(text: str) -> dict | None:
    """兼容 iputils(busybox) 两种 ping 统计输出：按 min/avg/max 标签取值。"""
    m = re.search(r"(?:min|round-trip)[^=\n]*?=\s*([\d.]+)/([\d.]+)/([\d.]+)", text, re.I)
    if m:
        return {"avg_ms": float(m.group(2)), "min_ms": float(m.group(1)),
                "max_ms": float(m.group(3)), "loss_pct": 0.0}
    loss = re.search(r"(\d+)% packet loss", text)
    if loss:
        return {"avg_ms": None, "min_ms": None, "max_ms": None, "loss_pct": float(loss.group(1))}
    return None


def measure_rtt(ssh: SSH, iface: str, target: str, count: int = 3) -> dict | None:
    """对指定接口 ping 目标，返回 {'avg_ms','loss_pct','min_ms','max_ms'}。"""
    cmd = f"ping -c {count} -i 0.2 -W 2 -I {iface} {target}"
    rc, out, err = ssh.run(cmd, timeout=30)
    text = out or err
    res = _parse_rtt(text)
    if res is None and rc != 0:
        rc2, out2, err2 = ssh.run(f"ping -c {count} -I {iface} {target}", timeout=30)
        res = _parse_rtt(out2 or err2)
        if res is None:
            text = out2 or err2
    if res is None:
        # 兜底：取该接口 IP，用 -I <IP> 再试（兼容不认接口名的 busybox）
        rc3, ipout, _ = ssh.run(
            f"ip -4 -o addr show dev {iface} 2>/dev/null | awk '{{print $4}}' | cut -d/ -f1 | head -1")
        ip = (ipout or "").strip().splitlines()[0] if ipout and ipout.strip() else ""
        if ip:
            rc4, out4, err4 = ssh.run(f"ping -c {count} -W 2 -I {ip} {target}", timeout=30)
            res = _parse_rtt(out4 or err4)
            if res is None:
                text = out4 or err4
    if res is None:
        return {"avg_ms": None, "min_ms": None, "max_ms": None, "loss_pct": 0.0,
                "raw": (text or err or "").strip()[-200:]}
    return res
