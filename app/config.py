"""配置读写：settings.json + 实验设计（多组）保存在配置文件中。"""
from __future__ import annotations

import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

DEFAULT_CONFIG = {
    "router": {"host": "", "port": 22, "user": "root", "password": "", "use_sudo": False},
    "vps": {"host": "", "port": 22, "user": "root", "password": "", "use_sudo": False},
    "iperf3": {"port": 5201, "udp_bitrate_mbps": 0, "mptcp": True},
    "last_links": [],
    "result_order": [],
    "groups": [
        {
            "name": "基线：链路1=100M/10ms + 链路2=50M/20ms",
            "gid": "1",
            "duration": 15,
            "protocol": "tcp",
            "udp_bitrate_mbps": 0,
            "shaping": "program",
            "links": [
                {"label": "宽带", "iface": "eth0", "rate_mbps": 100, "delay_ms": 10,
                 "jitter_ms": 0, "loss_pct": 0, "enabled": True},
                {"label": "4G", "iface": "eth1", "rate_mbps": 50, "delay_ms": 20,
                 "jitter_ms": 0, "loss_pct": 0, "enabled": True},
            ],
            "break": {"enabled": False, "iface": "eth1", "at_sec": 20, "restore_sec": 5},
        }
    ],
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _deep_merge(base[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SETTINGS_FILE):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return _deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), json.load(f))
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
