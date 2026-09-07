"""从 CORE 场景 / lagsim 配置文本中解析每路弱网参数。

返回统一结构:
    [{"rate_mbps": float|None, "delay_ms": float|None,
      "jitter_ms": float|None, "loss_pct": float|None}, ...]

解析是"尽力而为"的：不同版本的 CORE/lagsim 格式不同，若解析不准，
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


def _parse_core_text_links(text: str) -> list[dict]:
    """解析 CORE 文本(.imn)格式的 link 块。

    示例:
        link l2 {
            delay 20000        # 微秒 → 20ms
            nodes {n5 n1}
            bandwidth 100000000  # bps → 100M
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
            for key, pat in (("rate", r"^(?:bandwidth|rate|bw)\s+([\d.]+)\s*(\S*)$"),
                             ("delay", r"^delay\s+([\d.]+)\s*(\S*)$"),
                             ("jitter", r"^jitter\s+([\d.]+)\s*(\S*)$"),
                             ("loss", r"^loss\s+([\d.]+)\s*(\S*)$")):
                m = re.match(pat, s, re.I)
                if m and key not in cur:
                    cur[key] = {"num": float(m.group(1)), "unit": (m.group(2) or "")}
            if s == "}":
                if any(k in cur for k in ("rate", "delay", "jitter", "loss")):
                    r = cur.get("rate")
                    d = cur.get("delay")
                    j = cur.get("jitter")
                    lo = cur.get("loss")
                    rate = _normalize_rate((str(r["num"]) + r["unit"]) if r else None)
                    # 文本格式 delay/jitter 单位为微秒
                    d_ms = (d["num"] / 1000.0) if d else None
                    if d and d["unit"]:
                        d_ms = _normalize_ms(str(d["num"]) + d["unit"])
                    j_ms = (j["num"] / 1000.0) if j else None
                    if j and j["unit"]:
                        j_ms = _normalize_ms(str(j["num"]) + j["unit"])
                    lanes.append({"rate_mbps": rate, "delay_ms": d_ms,
                                  "jitter_ms": j_ms, "loss_pct": (lo["num"] if lo else None)})
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


def _split_blocks(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n|(?=^[ \t]*\[?[A-Za-z0-9_ -]{2,40}\]?\s*[:{])", text, flags=re.M)
    return [p for p in parts if p.strip()]


def parse_lagsim_lanes(text: str) -> list[dict]:
    """尽力解析 lagsim 的配置/profiles 文本，把每段数值提取成一路参数。"""
    lanes = []
    for blk in _split_blocks(text):
        line = blk.lower()
        if any(kw in line for kw in ("profile", "apply", "rule", "client", "interface")):
            lanes.append(_extract_lane(blk))
    if not lanes:
        lanes.append(_extract_lane(text))
    return lanes


def _extract_lane(blk: str) -> dict:
    def _by_keywords(keys):
        best = None
        for line in blk.splitlines():
            low = line.lower()
            for k in keys:
                if k in low:
                    val = re.sub(r"^[^=:]*[=:]\s*", "", line).strip()
                    if val and not val.lower().startswith(("true", "false", "yes", "no")):
                        best = val
                        break
            if best:
                break
        return best

    rate = _normalize_rate(_by_keywords(_KW_BW))
    delay = _normalize_ms(_by_keywords(_KW_DELAY))
    jitter = _normalize_ms(_by_keywords(_KW_JITTER))
    loss = _normalize_loss(_by_keywords(_KW_LOSS))
    return {"rate_mbps": rate, "delay_ms": delay, "jitter_ms": jitter, "loss_pct": loss}
