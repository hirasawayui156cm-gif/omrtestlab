"""tc（Linux Traffic Control）整形命令生成：对 WAN 接口设置带宽 / 时延 / 抖动 / 丢包。

- 限速：tbf（令牌桶）
- 时延/抖动/丢包：netem（作为 tbf 的子 qdisc，或直接作为根 qdisc）
"""
from __future__ import annotations


def _burst_kbit(rate_mbps: int) -> int:
    """根据限速计算一个合理的 tbf burst（约为 12ms 的流量，最小 32kbit）。"""
    return max(32, int(rate_mbps) * 12)


def apply_link_cmds(iface: str, rate_mbps: float | None, delay_ms: float = 0,
                    jitter_ms: float = 0, loss_pct: float = 0.0) -> list[str]:
    """生成对单个接口设置整形的命令列表（先清除旧规则再添加）。"""
    has_rate = rate_mbps and float(rate_mbps) > 0
    netem_parts = []
    if delay_ms and float(delay_ms) > 0:
        d = f"{float(delay_ms):g}ms"
        if jitter_ms and float(jitter_ms) > 0:
            d += f" {float(jitter_ms):g}ms"
        netem_parts.append("delay " + d)
    if loss_pct and float(loss_pct) > 0:
        netem_parts.append(f"loss {float(loss_pct):g}%")
    netem = " ".join(netem_parts)

    cmds = [f"tc qdisc del dev {iface} root 2>/dev/null; true"]
    if has_rate and netem:
        burst = _burst_kbit(int(float(rate_mbps)))
        cmds.append(f"tc qdisc add dev {iface} root handle 1: tbf rate {float(rate_mbps):g}mbit "
                    f"burst {burst}kbit latency 400ms")
        cmds.append(f"tc qdisc add dev {iface} parent 1:1 handle 10: netem {netem}")
    elif has_rate:
        burst = _burst_kbit(int(float(rate_mbps)))
        cmds.append(f"tc qdisc add dev {iface} root handle 1: tbf rate {float(rate_mbps):g}mbit "
                    f"burst {burst}kbit latency 400ms")
    elif netem:
        cmds.append(f"tc qdisc add dev {iface} root handle 10: netem {netem}")
    return cmds


def clear_link_cmds(iface: str) -> list[str]:
    return [f"tc qdisc del dev {iface} root 2>/dev/null; true"]


def show_link_cmds(iface: str) -> str:
    return f"tc qdisc show dev {iface}"
