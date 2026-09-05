"""FastAPI 服务：REST API + WebSocket 实时推送 + 静态页面。"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import threading

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import iperf
from .config import BASE_DIR, load_config, save_config
from .runner import Runner
from .ssh import SSH
from .storage import Storage

app = FastAPI(title="OMR 多链路聚合测试台")


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


app.add_middleware(NoCacheMiddleware)

static_dir = os.path.join(BASE_DIR, "app", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

CONFIG = load_config()
STORAGE = Storage()
RUNNER = Runner(CONFIG, STORAGE)

# ---------------- WebSocket 广播 ----------------
_ws_lock = threading.Lock()
_clients: list[WebSocket] = []
_loop_ref = None


def _get_loop():
    return _loop_ref


def broadcast(event: dict):
    loop = _get_loop()
    if not loop or not _clients:
        return
    with _ws_lock:
        for ws in list(_clients):
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(event), loop)
            except Exception:
                pass


RUNNER.on_event = broadcast


@app.on_event("startup")
async def _capture_loop():
    global _loop_ref
    _loop_ref = asyncio.get_running_loop()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    with _ws_lock:
        _clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            _clients.remove(ws)


# ---------------- 基础 API ----------------
@app.get("/")
def index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "running": RUNNER.running}


@app.get("/api/config")
def get_config():
    return CONFIG


@app.post("/api/config")
def set_config(cfg: dict):
    global CONFIG, RUNNER
    if RUNNER.running:
        return {"ok": False, "error": "测试运行中，无法修改配置"}
    CONFIG.clear()
    CONFIG.update(cfg)
    save_config(CONFIG)
    RUNNER.config = CONFIG
    return {"ok": True}


# ---------------- SSH / 环境检测 ----------------
def _ssh_for(which: str) -> SSH:
    h = CONFIG.get(which, {})
    return SSH(h.get("host"), h.get("port", 22), h.get("user"), h.get("password", ""),
               use_sudo=h.get("use_sudo", False))


@app.post("/api/check/{which}")
def check_ssh(which: str):
    if which not in ("router", "vps"):
        return {"ok": False, "error": "unknown"}
    h = CONFIG.get(which, {})
    if not h.get("host"):
        return {"ok": False, "error": "未填写主机地址"}
    try:
        ssh = _ssh_for(which)
        rc, out, err = ssh.run("echo OK; uname -a")
        ssh.close()
        return {"ok": True, "host": h["host"], "user": h["user"],
                "message": out.strip(), "error": err.strip()[:200] if rc != 0 else ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/interfaces")
def list_interfaces():
    try:
        ssh = _ssh_for("router")
        rc, out, err = ssh.run("ip -o link show | awk -F': ' '{print $2}'")
        ssh.close()
        ifaces = []
        for l in (x.strip() for x in out.splitlines()):
            if l and l != "lo":
                ifaces.append(l.split("@")[0])
        return {"ok": True, "interfaces": sorted(set(ifaces))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/prepare")
def prepare():
    results = {}
    try:
        r = iperf.ensure_client(_ssh_for("router"))
        results["router_iperf3"] = r
    except Exception as exc:
        results["router_iperf3"] = {"ok": False, "error": str(exc)}
    try:
        r = iperf.ensure_server(_ssh_for("vps"), int(CONFIG.get("iperf3", {}).get("port", 5201)))
        results["vps_iperf3"] = r
    except Exception as exc:
        results["vps_iperf3"] = {"ok": False, "error": str(exc)}
    return results


# ---------------- 实验组 ----------------
@app.get("/api/groups")
def get_groups():
    return {"ok": True, "groups": CONFIG.get("groups", [])}


@app.post("/api/groups")
def save_groups(body: dict):
    CONFIG["groups"] = body.get("groups", [])
    save_config(CONFIG)
    return {"ok": True, "count": len(CONFIG["groups"])}


# ---------------- 运行控制 ----------------
@app.post("/api/runs/start")
def start_runs(body: dict):
    if RUNNER.running:
        return {"ok": False, "error": "已有测试正在运行"}
    groups = body.get("groups")
    if groups is None:
        groups = CONFIG.get("groups", [])
    if not groups:
        return {"ok": False, "error": "没有可运行的实验组"}
    if not CONFIG.get("router", {}).get("host"):
        return {"ok": False, "error": "请先配置路由器 SSH"}
    if not CONFIG.get("vps", {}).get("host"):
        return {"ok": False, "error": "请先配置 VPS SSH"}
    RUNNER.start(groups)
    return {"ok": True}


@app.post("/api/runs/stop")
def stop_runs():
    RUNNER.stop()
    return {"ok": True}


@app.get("/api/runs")
def list_runs():
    return {"ok": True, "runs": STORAGE.list_all()}


@app.get("/api/runs/{rid}")
def get_run(rid: int):
    r = STORAGE.get(rid)
    return {"ok": r is not None, "run": r}


@app.delete("/api/runs")
def delete_runs():
    n = STORAGE.delete_all()
    return {"ok": True, "deleted": n}


@app.delete("/api/runs/{rid}")
def delete_run(rid: int):
    deleted = STORAGE.delete(rid)
    return {"ok": True, "deleted": deleted}


# ---------------- 导出 ----------------
@app.get("/api/export")
def export(fmt: str = "csv", ids: str = ""):
    runs = STORAGE.list_all()
    if ids:
        id_set = {int(x) for x in ids.split(",") if x.strip().isdigit()}
        runs = [r for r in runs if r.get("id") in id_set]
    if fmt == "json":
        return StreamingResponse(
            io.BytesIO(json.dumps(runs, ensure_ascii=False, indent=2).encode("utf-8")),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=omr_results.json"})
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "时间", "实验组", "整形", "协议", "时长s", "理论Mbps",
                     "上行Mbps", "下行Mbps", "上行效率%", "下行效率%",
                     "重传", "抖动ms", "丢包%", "恢复时间s"])
    for r in runs:
        writer.writerow([
            r.get("gid") or r.get("id"), r.get("ts"), r.get("group_name"),
            "程序tc" if r.get("shaping") != "core" else "CORE",
            r.get("protocol"), r.get("duration"), r.get("theory_mbps"),
            r.get("upload_mbps"), r.get("download_mbps"),
            r.get("efficiency_up_pct"), r.get("efficiency_down_pct"),
            r.get("retransmits"), r.get("jitter_ms"), r.get("lost_pct"),
            r.get("recovery_sec")])
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=omr_results.csv"})


def _chart_rows(ids: str = "") -> list[dict]:
    """生成对比图表用的扁平化数据集（每组一行，含各链路负载/时延列）。"""
    runs = STORAGE.list_all()
    if ids:
        id_set = {int(x) for x in ids.split(",") if x.strip().isdigit()}
        runs = [r for r in runs if r.get("id") in id_set]
    ifaces: list[str] = []
    for r in runs:
        for k in (r.get("per_link") or {}):
            if k not in ifaces:
                ifaces.append(k)
    rows = []
    for r in runs:
        pl = r.get("per_link") or {}
        row = {
            "ID": r.get("gid") or r.get("id"), "时间": r.get("ts"), "实验组": r.get("group_name"),
            "整形": "程序tc" if r.get("shaping") != "core" else "CORE",
            "协议": r.get("protocol"), "时长s": r.get("duration"),
            "理论Mbps": r.get("theory_mbps"), "上行Mbps": r.get("upload_mbps"),
            "下行Mbps": r.get("download_mbps"),
            "上行效率%": r.get("efficiency_up_pct"), "下行效率%": r.get("efficiency_down_pct"),
            "重传": r.get("retransmits"), "抖动ms": r.get("jitter_ms"), "丢包%": r.get("lost_pct"),
        }
        for f in ifaces:
            p = pl.get(f) or {}
            rtt = p.get("rtt") or {}
            row[f"{f}_上行峰值Mbps"] = p.get("tx_max_mbps")
            row[f"{f}_下行峰值Mbps"] = p.get("rx_max_mbps")
            row[f"{f}_时延ms"] = rtt.get("avg_ms")
        rows.append(row)
    return rows


@app.get("/api/export-charts")
def export_charts(fmt: str = "csv", ids: str = ""):
    rows = _chart_rows(ids)
    if not rows:
        return {"ok": False, "error": "暂无结果可导出"}
    if fmt == "json":
        return StreamingResponse(
            io.BytesIO(json.dumps(rows, ensure_ascii=False, indent=2, default=str).encode("utf-8")),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=omr_charts.json"})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=omr_charts.csv"})
