"""从 CORE 场景配置文本中解析每路弱网参数。

返回统一结构:
    [{"rate_mbps": float|None, "delay_ms": float|None,
      "jitter_ms": float|None, "loss_pct": float|None}, ...]

解析是"尽力而为"的：不同版本的 CORE 格式不同，若解析不准，
导入后的数值会在界面上可直接手改（导入只做预填）。
"""
from __future__ import annotations

import re

_KW_BW = ("bandwidth", "bw", "rate", "bitrate", "bandwidth_limit", "limit")
_KW_DELAY = ("delay", "latency")
_KW_JITTER = ("jitter", "delay-jitter", "latency_jitter")
_KW_LOSS = ("loss", "loss_pct", "loss%", "packetloss", "drop")


def _num(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(s))
    return float(m.group()) if m else None


def _normalize_rate(raw) -> float | None:
    """带宽 → Mbps。兼容 '100000000'(bps)/'100mbit'/'100M'/'100m'/'100k' 等。"""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    m = re.match(r"([\d.]+)\s*(mbit|mbps|m|kbit|kbps|k|gbps|gbit|g|bps)?", s)
    if not m:
        return None
    v = float(m.group(1))
    u = (m.group(2) or "").replace("bps", "").replace("bit", "")
    if u in ("g",):
        return v * 1000.0
    if u in ("k",):
        return v / 1000.0
    if u in ("m",):
        return v
    if u == "":
        # 无单位：CORE 常见以 bps 存储（如 50000000=50M）
        return v / 1_000_000.0 if v >= 10000 else v
    return v


def _normalize_ms(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    m = re.search(r"([\d.]+)\s*(ms|msec|s|µs|us)?", s)
    if not m:
        return None
    v = float(m.group(1))
    u = (m.group(2) or "").strip()
    if u in ("s",):
        return v * 1000.0
    if u in ("us", "µs"):
        return v / 1000.0
    return v


def _normalize_loss(raw) -> float | None:
    if raw is None:
        return None
    v = _num(raw)
    return None if v is None else v


def _pick_attrs(attrs: dict, keys: tuple) -> str | None:
    for k in keys:
        for ak, av in attrs.items():
            if ak.lower().replace("_", "") == k.lower().replace("_", ""):
                return av
    return None


def _find_links(text: str) -> list[str]:
    """粗切出每条 <link ...> 块（含自闭合与成对两种）。"""
    blocks = []
    for m in re.finditer(r"<link\b[^>]*?(/?)>", text, flags=re.I):
        seg = m.group(0)
        if not m.group(1):
            close = text.find("</link", m.end())
            if close != -1:
                seg = text[m.start():text.find(">", close) + 1]
        blocks.append(seg)
    return blocks


def _num_list(line: str) -> list[float]:
    """提取一行里的数值：支持标量 'delay 20000' 或矢量 'bandwidth {a b}'。"""
    b = re.search(r"\{\s*([^}]*)\}", line)
    if b:
        return [float(x) for x in re.findall(r"[\d.]+", b.group(1))]
    m = re.search(r"[\d.]+", line)
    return [float(m.group())] if m else []


def _to_rate(v: float) -> float | None:
    return v / 1_000_000.0  # 文本格式 bandwidth 单位为 bps


def _to_ms(v: float) -> float:
    return v / 1000.0  # 文本格式 delay/jitter 单位为微秒


def _parse_core_text_links(text: str) -> list[dict]:
    """解析 CORE 文本(.imn)格式的 link 块（支持标量/不对称矢量）。

    CORE 矢量语义：{下行 上行}，例如
        link l2 {
            delay {5000 5000}          # 双向 5ms（微秒）
            nodes {n1 n5}
            bandwidth {50000000 20000000}  # 下行50M / 上行20M（bps）
            jitter {1000 1000}
            ber {0.1 0.05}
        }
    """
    lanes = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if cur is None and re.match(r"^link\s+\S+\s*\{?$", s):
            cur = {}
            continue
        if cur is not None:
            m = re.match(r"^(bandwidth|rate|bw|delay|jitter|loss|ber)\b", s, re.I)
            if m:
                key = m.group(1).lower()
                if key in ("bandwidth", "rate", "bw"):
                    key = "rate"
                elif key == "ber":
                    key = "loss"
                if key not in cur:
                    cur[key] = _num_list(s)
            if s == "}":
                lane = {}
                for k, cv in (("rate", _to_rate), ("delay", _to_ms),
                              ("jitter", _to_ms), ("loss", lambda x: x)):
                    if k in cur and cur[k]:
                        vals = cur[k]
                        # 矢量 = {下行 上行}；标量 = 双向同值
                        dn, up = vals[0], (vals[1] if len(vals) > 1 else vals[0])
                        suf = "mbps" if k == "rate" else ("ms" if k in ("delay", "jitter") else "pct")
                        lane[k + "_" + suf] = cv(up)      # 上行(用户主字段)
                        lane["dl_" + k + "_" + suf] = cv(dn)  # 下行
                        if len(vals) > 1:
                            lane["asym"] = True
                if lane:
                    lanes.append(lane)
                cur = None
    return lanes


def parse_core_links(text: str) -> list[dict]:
    """解析 CORE 场景：优先 XML，其次文本(.imn)格式。"""
    if re.search(r"^\s*link\s+\S+\s*\{", text, flags=re.M):
        return _parse_core_text_links(text)
    return _parse_xml_links(text)


def _parse_xml_links(text: str) -> list[dict]:
    lanes = []
    for blk in _find_links(text):
        attrs = {}
        for am in re.finditer(r'(\w[\w:.-]*)\s*=\s*"([^"]*)"', blk):
            attrs[am.group(1)] = am.group(2)
        raw = _pick_attrs(attrs, _KW_BW)
        delay = _pick_attrs(attrs, _KW_DELAY)
        jitter = _pick_attrs(attrs, _KW_JITTER)
        loss = _pick_attrs(attrs, _KW_LOSS)
        lanes.append({
            "rate_mbps": _normalize_rate(raw),
            "delay_ms": _normalize_ms(delay),
            "jitter_ms": _normalize_ms(jitter),
            "loss_pct": _normalize_loss(loss),
        })
    return lanes


# ---------------- 写回 .imn ----------------
_PARAM_KEYS = ("bandwidth", "ber", "delay", "jitter")


def _fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.6g}"


def _pair(first, second) -> str:
    return f"{{{_fmt_num(first)} {_fmt_num(second)}}}"


def _lane_pair(lane: dict, up_key: str, dn_key: str, conv) -> str | None:
    """按 CORE 语义 {下行 上行} 输出：先下行(dl_*)，后上行(*)。"""
    up = lane.get(up_key)
    if up is None:
        return None
    dn = lane.get(dn_key)
    if dn is None:
        dn = up
    return _pair(conv(dn), conv(up))


def write_core_links(text: str, lanes: list[dict]) -> str:
    """把参数写回 CORE 文本(.imn)：更新"已有整形参数"的 link 块，顺序对应 lanes。

    参数向量按 CORE 语义写成 {下行 上行}，如:
        jitter {1000 1000}
        ber {0.1 0.05}
        delay {5000 5000}
        bandwidth {100000000 20000000}   # 下行100M / 上行20M
    """
    if not re.search(r"^\s*link\s+\S+\s*\{", text, flags=re.M):
        raise ValueError("仅支持 CORE 文本(.imn) 格式")

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    idx = 0  # 已更新的整形链路序号
    n = len(lines)
    while i < n:
        line = lines[i]
        m = re.match(r"^link\s+(\S+)\s*\{", line)
        if m:
            block = [line]
            i += 1
            while i < n and not re.match(r"^\s*\}", lines[i]):
                block.append(lines[i])
                i += 1
            if i < n:
                block.append(lines[i])  # '}' 行
            i += 1
            shaped = any(re.match(r"^\s*(" + "|".join(_PARAM_KEYS) + r")\b", b) for b in block)
            if shaped and idx < len(lanes):
                lane = lanes[idx]
                idx += 1
                params = []
                # 按示例顺序写：jitter / ber / delay / bandwidth（缺失的补上，双向同值/不同值）
                def _pv(up_key, dn_key, conv):
                    s = _lane_pair(lane, up_key, dn_key, conv)
                    return s

                pv = []
                for key, upk, dnk, conv in (
                        ("jitter", "jitter_ms", "dl_jitter_ms", lambda x: round(float(x) * 1000.0)),
                        ("ber", "loss_pct", "dl_loss_pct", float),
                        ("delay", "delay_ms", "dl_delay_ms", lambda x: round(float(x) * 1000.0)),
                        ("bandwidth", "rate_mbps", "dl_rate_mbps", lambda x: round(float(x) * 1_000_000.0))):
                    up = lane.get(upk)
                    dn = lane.get(dnk)
                    if up is None:
                        continue
                    if dn is None:
                        dn = up
                    # 上/下均为 0 的参数就不写该行（不给 0 占位）
                    if up == 0 and dn == 0:
                        continue
                    pv.append(f"{key} {_pair(conv(dn), conv(up))}")
                # 去掉原有 param 行，保留其余(nodes 等)
                rest = [b for b in block if not re.match(r"^\s*(" + "|".join(_PARAM_KEYS) + r")\b", b)]
                indent = "    "
                for b in block:
                    mm = re.match(r"^(\s*)(" + "|".join(_PARAM_KEYS) + r")\b", b)
                    if mm:
                        indent = mm.group(1)
                        break
                if not pv:
                    new_block = block
                else:
                    param_lines = [indent + p + "\n" for p in pv]
                    insert_at = None
                    for j, b in enumerate(rest):
                        if re.match(r"^\s*nodes\b", b):
                            insert_at = j
                            break
                    if insert_at is None:
                        new_block = rest[:-1] + param_lines + rest[-1:]
                    else:
                        new_block = rest[:insert_at] + param_lines + rest[insert_at:]
                out.extend(new_block)
            else:
                out.extend(block)
        else:
            out.append(line)
            i += 1
    if idx == 0:
        raise ValueError("场景文件中未找到带参数的整形链路(link)")
    return "".join(out)

