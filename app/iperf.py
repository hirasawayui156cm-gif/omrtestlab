"""iperf3 客户端执行与结果解析。

在路由器上执行 iperf3 客户端（流量自然走聚合隧道），服务端位于 VPS。
返回结构化结果：吞吐量、重传、时延/丢包（UDP）、逐秒时间线。
"""
from __future__ import annotations

import json
import re

from .ssh import SSH, SSHStream


def _find_json(text: str) -> str:
    idx = text.find("{")
    return text[idx:] if idx >= 0 else ""


def _mbps(v: float) -> float:
    return round(v / 1_000_000.0, 3)


def _parse_intervals(data: dict, reverse: bool) -> list[dict]:
    """从 iperf3 JSON 提取逐秒时间线（t 为累计秒，兼容 TCP/UDP）。"""
    out = []
    t = 0.0
    for it in data.get("intervals", []):
        s = it.get("streams", [{}])[0]
        secs = float(it.get("sum", {}).get("seconds") or 1.0)
        t += secs
        bps = it.get("sum", {}).get("bits_per_second")
        if bps is None:
            bps = (it.get("sum_received") or {}).get("bits_per_second")
        if bps is None:
            bps = s.get("sum_received", {}).get("bits_per_second") if reverse else s.get("sum", {}).get("bits_per_second")
        if bps is None:
            # UDP：无 bits_per_second 字段，用字节数换算
            by = (it.get("sum") or s.get("sum") or {}).get("bytes", 0)
            bps = by * 8.0 / secs
        retr = it.get("sum", {}).get("retransmits") or 0
        out.append({"t": round(t, 2),
                    "mbps": round((bps or 0) / 1_000_000.0, 3),
                    "retransmits": retr})
    return out


def _bps(side: dict, duration_s: float) -> float:
    bps = side.get("bits_per_second")
    if bps is None:
        by = side.get("bytes", 0)
        bps = by * 8.0 / duration_s if duration_s > 0 else 0.0
    return bps


def _parse_result(rc: int, out: str, err: str, reverse: bool) -> dict:
    if rc != 0 and not out:
        return {"ok": False, "error": err.strip()[:400] or f"iperf3 退出码 {rc}"}
    raw = _find_json(out)
    try:
        data = json.loads(raw)
    except Exception:
        return {"ok": False, "error": f"无法解析 iperf3 输出: {out[:400]}\n{err[:200]}"}

    end = data.get("end", {})
    dur = float((data.get("start", {}).get("test_start", {}) or {}).get("duration") or 0)
    sum_sent = end.get("sum_sent", {}) or {}
    sum_recv = end.get("sum_received", {}) or {}
    result = {
        "ok": True,
        "is_udp": bool(data.get("start", {}).get("test_start", {}).get("udp")),
        "upload_mbps": round(_bps(sum_sent, dur) / 1_000_000.0, 3),
        "download_mbps": round(_bps(sum_recv, dur) / 1_000_000.0, 3),
        "retransmits": sum_sent.get("retransmits", 0),
        "jitter_ms": round((sum_recv.get("jitter_ms") or 0), 3),
        "lost_pct": round((sum_recv.get("lost_percent") or 0), 3),
        "timeline": _parse_intervals(data, reverse),
        "raw": out[-2000:],
    }
    return result


def run_iperf3(ssh: SSH, server: str, port: int, duration: int,
               reverse: bool = False, udp: bool = False,
               udp_bitrate_mbps: int = 0, interval: int = 1,
               stop_event=None, mptcp: bool = True) -> dict:
    """在远程主机上执行一次 iperf3。返回 dict 结果。"""
    cmd = f"iperf3 -J -c {server} -p {port} -t {duration} -i {interval}"
    if reverse:
        cmd += " -R"
    if udp:
        cmd += " -u"
        cmd += f" -b {udp_bitrate_mbps or 500}m"
    elif mptcp:
        cmd += " --mptcp"
    stream: SSHStream = ssh.exec_stream(cmd)
    chunks = []
    while True:
        line = stream.readline(timeout=15)
        if line is None:          # 被 stop() 中止
            break
        if line == "":
            break
        chunks.append(line)
        if stop_event and stop_event.is_set():
            stream.stop()
            break
    out = "\n".join(chunks)
    err_txt = ""
    try:
        err_txt = stream.stderr.read().decode("utf-8", "replace")
    except Exception:
        err_txt = ""
    rc = 0
    if stop_event and stop_event.is_set():
        return {"ok": False, "error": "用户中止"}
    try:
        rc = stream.stdout.channel.recv_exit_status()
    except Exception:
        rc = 0
    if not out and err_txt:
        out = err_txt  # 错误信息常打到 stderr，让它进解析/报错
    return _parse_result(rc, out, err_txt, reverse)


def ensure_client(ssh: SSH) -> dict:
    """检查路由器上是否安装了 iperf3，没有则尝试安装；并报告 MPTCP 能力。"""
    rc, out, _ = ssh.run("which iperf3 2>/dev/null || command -v iperf3")
    if rc == 0 and out.strip():
        msg = f"iperf3 已安装: {out.strip()}"
    else:
        rc, out, err = ssh.run(
            "opkg update >/dev/null 2>&1 && opkg install iperf3 2>&1 | tail -2; "
            "command -v iperf3 && echo INSTALL_OK")
        if rc == 0 and "INSTALL_OK" in out:
            msg = "iperf3 安装成功"
        else:
            return {"ok": False, "error": f"iperf3 不可用且安装失败: {err.strip()[:200]} {out.strip()[:200]}"}
    rc2, out2, _ = ssh.run("iperf3 --help 2>&1 | grep -q -- '--mptcp' && echo YES || echo NO")
    mptcp_ok = "YES" in out2
    return {"ok": True, "message": msg, "mptcp": mptcp_ok}


def _listen_cmd(port: int) -> str:
    return f"ss -lntu | grep -q ':{port} ' && echo LISTEN || echo NO"


def ensure_server(ssh: SSH, port: int) -> dict:
    """检查 VPS 上 iperf3 是否存在并启动服务端。"""
    rc, out, _ = ssh.run("command -v iperf3")
    if rc != 0:
        rc2, out2, err2 = ssh.run(
            "apt-get update >/dev/null 2>&1; apt-get install -y iperf3 2>&1 | tail -2; command -v iperf3 && echo OK")
        if "OK" not in out2:
            return {"ok": False, "error": f"VPS 安装 iperf3 失败: {err2.strip()[:200]}"}
    rc3, out3, _ = ssh.run("iperf3 --help 2>&1 | grep -q -- '--mptcp' && echo YES || echo NO")
    mptcp_ok = "YES" in out3
    mptcp_flag = " --mptcp" if mptcp_ok else ""
    # 统一先杀掉旧服务端再启动，保证跑的是当前这份（含 --mptcp 能力）
    ssh.run("pkill -9 -f 'iperf3 -s'; sleep 1")
    ssh.run(f"setsid nohup iperf3 -s -p {port}{mptcp_flag} </dev/null "
            f">/tmp/omr_iperf3.log 2>&1 & sleep 1; true")
    rc2, out2, _ = ssh.run(f"sleep 1; {_listen_cmd(port)}")
    if "LISTEN" not in out2:
        rc3, out3, err3 = ssh.run("cat /tmp/omr_iperf3.log 2>/dev/null | tail -3")
        detail = out3.strip() or err3.strip() or "未知错误"
        ssh.run(f"pkill -9 -f 'iperf3 -s' ; sleep 1; "
                f"setsid nohup iperf3 -s -p {port} </dev/null "
                f">/tmp/omr_iperf3.log 2>&1 & sleep 1; true")
        rc4, out4, _ = ssh.run(f"sleep 1; {_listen_cmd(port)}")
        if "LISTEN" not in out4:
            return {"ok": False,
                    "error": f"iperf3 服务端启动失败: {detail[:200]}"}
    msg = f"iperf3 服务端已就绪 (端口 {port}{'，--mptcp' if mptcp_ok else ''})"
    return {"ok": True, "message": msg, "mptcp": mptcp_ok}
