"""后台测试运行器：负责按实验组依次执行 整形->测速->采集->统计->清理->入库。"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from datetime import datetime

from . import tc
from .iperf import run_iperf3
from .monitor import ThroughputMonitor, measure_rtt
from .ssh import SSH


class Runner:
    def __init__(self, config: dict, storage, on_event=None):
        self.config = config
        self.storage = storage
        self.on_event = on_event or (lambda e: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ssh: SSH | None = None
        self._vps_ssh: SSH | None = None
        self._shaper_ssh: SSH | None = None
        self._vps_shaped: list[str] = []
        self._broken_ifaces: list[str] = []
        self._did_shape = False

    def _ensure_vps(self):
        if self._vps_ssh:
            return
        v = self.config.get("vps", {})
        if not v.get("host"):
            self._log("warn", "未配置 VPS SSH，无法做下行整形")
            return
        try:
            self._vps_ssh = SSH(v.get("host"), v.get("port", 22), v.get("user"),
                                v.get("password", ""), use_sudo=v.get("use_sudo", False))
        except Exception as exc:
            self._log("warn", f"连接 VPS(下行整形)失败: {exc}")
            self._vps_ssh = None

    # ---------- 事件 ----------
    def _emit(self, etype: str, **kw):
        e = {"type": etype, "ts": datetime.now().strftime("%H:%M:%S"), **kw}
        try:
            self.on_event(e)
        except Exception:
            pass

    def _log(self, level: str, msg: str):
        self._emit("log", level=level, msg=msg)

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, groups: list[dict]):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_all, args=(groups,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._log("warn", "收到停止请求，正在中止当前测试…")

    def _cleanup(self):
        cfg = self.config.get("router", {})
        for iface in self.config.get("last_links", []):
            for c in tc.clear_link_cmds(iface):
                try:
                    self._ssh.run(c, timeout=10)
                except Exception:
                    pass
        if self._vps_shaped and self._vps_ssh:
            for viface in self._vps_shaped:
                for c in tc.clear_link_cmds(viface):
                    try:
                        self._vps_ssh.run(c, timeout=10)
                    except Exception:
                        pass
            self._vps_shaped = []
        for iface in list(self._broken_ifaces):
            try:
                self._ssh.run(f"ip link set {iface} up", timeout=10)
            except Exception:
                pass
        self._broken_ifaces = []
        if self._vps_ssh:
            try:
                self._vps_ssh.close()
            except Exception:
                pass
            self._vps_ssh = None
        if getattr(self, "_shaper_ssh", None):
            try:
                self._shaper_ssh.close()
            except Exception:
                pass
            self._shaper_ssh = None
        if self._ssh:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

    # ---------- 主流程 ----------
    def _run_all(self, groups: list[dict]):
        try:
            router = self.config.get("router", {})
            self._ssh = SSH(router.get("host"), router.get("port", 22),
                            router.get("user"), router.get("password", ""),
                            use_sudo=router.get("use_sudo", False))
            total = len(groups)
            for gi, group in enumerate(groups):
                if self._stop.is_set():
                    break
                self._emit("group_start", index=gi, total=total, name=group.get("name", "未命名"))
                try:
                    result = self._run_group(group, gi, total)
                    if result:
                        rid = self.storage.save(result)
                        result["id"] = rid
                        self._emit("group_done", result=result)
                except Exception as exc:
                    self._log("error", f"实验组 [{group.get('name')}] 失败: {exc}\n{traceback.format_exc(limit=3)}")
                finally:
                    self._cleanup_links()
        except Exception as exc:
            self._log("error", f"运行器异常: {exc}\n{traceback.format_exc(limit=3)}")
        finally:
            self._cleanup()
            self._emit("all_done")

    def _cleanup_links(self):
        if not self._ssh:
            return
        for iface in self.config.get("last_links", []):
            for c in tc.clear_link_cmds(iface):
                try:
                    self._ssh.run(c, timeout=10)
                except Exception:
                    pass
        # 恢复 OMR 默认根 qdisc(fq/pacing)等，避免程序tc 改变 BPI 行为后残留
        if self._did_shape:
            try:
                self._log("info", "恢复 OMR 网络默认( mptcp reload )…")
                self._ssh.run("/etc/init.d/mptcp reload >/dev/null 2>&1; true", timeout=90)
            except Exception as exc:
                self._log("warn", f"恢复 mptcp 默认失败: {exc}")
            self._did_shape = False
        # 清除 VPS 侧下行整形
        if self._vps_shaped and self._vps_ssh:
            for viface in self._vps_shaped:
                for c in tc.clear_link_cmds(viface):
                    try:
                        self._vps_ssh.run(c, timeout=10)
                    except Exception:
                        pass
            self._vps_shaped = []
        # 确保断链的接口都恢复
        for iface in list(self._broken_ifaces):
            try:
                self._ssh.run(f"ip link set {iface} up", timeout=10)
            except Exception:
                pass
        self._broken_ifaces = []

    def _ensure_shaper(self):
        if getattr(self, "_shaper_ssh", None):
            return
        sh = self.config.get("shaper", {})
        if not sh.get("host"):
            self._shaper_ssh = None
            return
        try:
            self._shaper_ssh = SSH(sh.get("host"), sh.get("port", 22), sh.get("user"),
                                   sh.get("password", ""), use_sudo=sh.get("use_sudo", False))
        except Exception as exc:
            self._log("warn", f"连接整形网关失败: {exc}")
            self._shaper_ssh = None

    def _apply_core_live(self, links: list[dict]):
        cl = self.config.get("core_live", {}) or {}
        if not cl.get("enabled"):
            return
        self._ensure_shaper()
        if not self._shaper_ssh:
            self._log("warn", "CORE实时应用：未配置/连不上整形网关，跳过")
            return
        mapping = cl.get("map") or {}
        tpl = cl.get("cmd_template") or (
            "coresendmsg link n1_number={n1} n2_number={n2} iface1_number={i1} "
            "iface2_number={i2} delay={delay} jitter={jitter} loss={loss} dup={dup} bandwidth={bw}")
        dup = cl.get("dup", 0)

        def _cmd(parts, i1, i2, delay_ms, jitter_ms, loss_pct, rate_mbps):
            return tpl.format(n1=parts[0], n2=parts[1], i1=i1, i2=i2,
                              delay=int(round(float(delay_ms or 0) * 1000)),
                              jitter=int(round(float(jitter_ms or 0) * 1000)),
                              loss=float(loss_pct or 0), dup=dup,
                              bw=int(round(float(rate_mbps or 0) * 1_000_000)))

        for l in links:
            parts = (mapping.get(l.get("iface")) or "").replace(" ", "").split(",")
            if len(parts) < 4:
                self._log("warn", f"  {l.get('iface')} 无 coresendmsg 映射，跳过")
                continue
            cmds = [_cmd(parts, parts[2], parts[3], l.get("delay_ms"), l.get("jitter_ms"),
                         l.get("loss_pct"), l.get("rate_mbps"))]
            if any(l.get(k) is not None for k in ("dl_delay_ms", "dl_jitter_ms", "dl_loss_pct", "dl_rate_mbps")):
                cmds.append(_cmd(parts, parts[3], parts[2], l.get("dl_delay_ms"), l.get("dl_jitter_ms"),
                                 l.get("dl_loss_pct"), l.get("dl_rate_mbps")))
            for cmd in cmds:
                rc, out, err = self._shaper_ssh.run(cmd, timeout=20)
                self._log("info" if rc == 0 else "warn",
                          f"  CORE实时 {l.get('iface')}: rc={rc} {cmd}")

    def _run_group(self, group: dict, gi: int, total: int) -> dict | None:
        cfg = self.config
        iperf_cfg = cfg.get("iperf3", {})
        server = cfg.get("vps", {}).get("host", "")
        port = int(iperf_cfg.get("port", 5201))
        use_mptcp = iperf_cfg.get("mptcp", True) is not False
        duration = max(5, int(group.get("duration", 30)))
        protocol = group.get("protocol", "tcp")
        udp = protocol == "udp"
        udp_bitrate = int(group.get("udp_bitrate_mbps", 0) or 0)

        links = [l for l in group.get("links", []) if l.get("enabled")]
        if not links:
            self._log("error", f"[{group['name']}] 没有启用的链路，跳过。")
            return None
        if not server:
            self._log("error", "[group] 未配置 VPS 地址，跳过。")
            return None
        # 补全下行参数：未单独设置时默认=上行参数
        for l in links:
            for k, dk in (("rate_mbps", "dl_rate_mbps"), ("delay_ms", "dl_delay_ms"),
                          ("jitter_ms", "dl_jitter_ms"), ("loss_pct", "dl_loss_pct")):
                if l.get(dk) is None:
                    l[dk] = l.get(k, 0)

        ifaces = [l["iface"] for l in links]
        cfg["last_links"] = ifaces
        shaping_mode = group.get("shaping", "program")
        shaping_on = shaping_mode in ("", "program", "tc")

        # 1) 施加整形（program 模式）或跳过（core 模式：由 CORE 整形，速率仅作参考）
        if shaping_on:
            self._did_shape = True
            self._log("info", f"[{group['name']}] 应用 tc 整形:")
            for l in links:
                cmds = tc.apply_link_cmds(l["iface"], l.get("rate_mbps"), l.get("delay_ms", 0),
                                          l.get("jitter_ms", 0), l.get("loss_pct", 0))
                for c in cmds:
                    rc, out, err = self._ssh.run(c, timeout=15)
                    if rc != 0 and "No such file" not in err:
                        self._log("warn", f"  {l['iface']}: {c} -> rc={rc} {err.strip()[:120]}")
                    else:
                        self._log("info", f"  {l['iface']}: {c} ok")
                self._log("info", f"  → {l['iface']} 限速={l.get('rate_mbps') or '无'}Mbit "
                                  f"时延={l.get('delay_ms',0)}ms 抖动={l.get('jitter_ms',0)}ms "
                                  f"丢包={l.get('loss_pct',0)}%")
            # 下行整形：在 VPS 对应出口打同样的 tc（双向同参数）
            dn = cfg.get("vps_downlink") or {}
            dn_map = dn.get("map") or {}
            if dn.get("enabled") and any(dn_map.get(l["iface"]) for l in links):
                self._ensure_vps()
                if self._vps_ssh:
                    self._log("info", f"[{group['name']}] 下行整形（VPS 出口）:")
                    self._vps_shaped = []
                    for l in links:
                        viface = dn_map.get(l["iface"])
                        if not viface:
                            self._log("warn", f"  {l['iface']} 无映射，跳过下行整形")
                            continue
                        cmds = tc.apply_link_cmds(viface, l.get("dl_rate_mbps"), l.get("dl_delay_ms", 0),
                                                  l.get("dl_jitter_ms", 0), l.get("dl_loss_pct", 0))
                        for c in cmds:
                            rc, out, err = self._vps_ssh.run(c, timeout=15)
                            self._log("info" if rc == 0 else "warn",
                                      f"  {viface}: {c} " + ("ok" if rc == 0 else f"rc={rc} {err.strip()[:100]}"))
                        self._vps_shaped.append(viface)
            time.sleep(1.0)
        else:
            self._log("info", f"[{group['name']}] CORE整形模式：跳过 tc，"
                              f"速率仅作理论参考（theory={sum(float(l.get('rate_mbps') or 0) for l in links)}Mbps）")
            self._apply_core_live(links)

        # 2) 断链计划（可选，容灾测试）：支持多条/多接口/同一时刻断多条
        breaks = self._normalize_breaks(group)
        self._broken_ifaces = []
        for b in breaks:
            if b.get("at_sec", 0) > 0 and b.get("ifaces") and not self._stop.is_set():
                threading.Thread(target=self._do_break_multi, args=(b,), daemon=True).start()
        break_cfg = breaks[0] if breaks else {}

        # 3) 吞吐实时监控（覆盖整段测速）
        theory_mbps = sum(float(l.get("rate_mbps") or 0) for l in links) or 0
        theory_down_mbps = sum(float(l.get("dl_rate_mbps") or 0) for l in links) or 0
        monitor_total = duration * 2 + 6 if not udp else duration + 6
        live_accum = {"up": [], "down": []}
        monitor = ThroughputMonitor(self._ssh, ifaces, monitor_total,
                                    on_sample=lambda s: self._emit("live", sample=s))
        monitor.start()
        self._log("info", f"[{group['name']}] 开始 {protocol.upper()} 测速，时长 {duration}s×"
                          f"{1 if udp else 2}，理论带宽 {theory_mbps}Mbps"
                          + ("" if udp else f"，{'--mptcp' if use_mptcp else '普通TCP'}"))

        # 4) 测速（上行 + 下行）
        up, down = None, None
        if not udp:
            up = run_iperf3(self._ssh, server, port, duration, reverse=False,
                            udp=False, stop_event=self._stop, mptcp=use_mptcp)
            if up.get("ok"):
                self._emit("phase", name="up", mbps=up.get("upload_mbps", 0),
                           timeline=up.get("timeline", []))
                live_accum["up"] = up["timeline"]
            else:
                self._log("error", f"[{group['name']}] 上行测速失败: {up.get('error')}")
            if self._stop.is_set():
                monitor.stop(); monitor.join(timeout=2)
                return None
            down = run_iperf3(self._ssh, server, port, duration, reverse=True,
                              udp=False, stop_event=self._stop, mptcp=use_mptcp)
            if down.get("ok"):
                self._emit("phase", name="down", mbps=down.get("download_mbps", 0),
                           timeline=down.get("timeline", []))
                live_accum["down"] = down["timeline"]
            else:
                self._log("error", f"[{group['name']}] 下行测速失败: {down.get('error')}")
        else:
            up = run_iperf3(self._ssh, server, port, duration, reverse=True, udp=True,
                            udp_bitrate_mbps=udp_bitrate or int(theory_mbps or 100),
                            stop_event=self._stop)
            if up.get("ok"):
                self._emit("phase", name="down", mbps=up.get("download_mbps", 0),
                           timeline=up.get("timeline", []))
                live_accum["down"] = up["timeline"]
            else:
                self._log("error", f"[{group['name']}] UDP(反向)测速失败: {up.get('error')}")

        # 5) 各链路 RTT
        per_link_rtt = {}
        if not self._stop.is_set():
            self._log("info", f"[{group['name']}] 测量各链路 RTT…")
            for l in links:
                r = measure_rtt(self._ssh, l["iface"], server, count=3)
                per_link_rtt[l["iface"]] = r or {"avg_ms": None}
                self._log("info", f"  {l['iface']}: {r}")

        # 6) 停止监控，汇总
        monitor.stop()
        monitor.join(timeout=3)
        per_link = monitor.summary()
        for iface in per_link:
            per_link[iface]["rtt"] = per_link_rtt.get(iface)

        up_mbps = (up or {}).get("upload_mbps", 0) or 0
        down_mbps = (down or {}).get("download_mbps", 0) or 0
        if udp:
            # UDP 反向：数据由服务端(VPS)发给客户端(BPI)，"下行"=实际接收速率
            up_mbps = down_mbps = (up or {}).get("download_mbps", 0) or 0

        result = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "group_name": group.get("name", "未命名"),
            "gid": str(group.get("gid") or ""),
            "shaping": "program" if shaping_on else "core",
            "duration": duration,
            "protocol": protocol,
            "links": links,
            "theory_mbps": round(theory_mbps, 3),
            "theory_down_mbps": round(theory_down_mbps, 3),
            "upload_mbps": round(up_mbps, 3),
            "download_mbps": round(down_mbps, 3),
            "efficiency_up_pct": round(up_mbps / theory_mbps * 100, 1) if theory_mbps else 0,
            "efficiency_down_pct": round(down_mbps / theory_down_mbps * 100, 1) if theory_down_mbps else 0,
            "retransmits": (up or {}).get("retransmits", 0),
            "jitter_ms": (up or {}).get("jitter_ms", 0),
            "lost_pct": (up or {}).get("lost_pct", 0),
            "per_link": per_link,
            "timeline_up": (up or {}).get("timeline", []),
            "timeline_down": (down or {}).get("timeline", []),
            "monitor": monitor.samples,
            "breaks": breaks,
            "break": break_cfg,
            "recovery_sec": None,
        }

        # 7) 断链恢复时间估算（用第一个"有恢复间隔"的断链条目）
        first = next((b for b in breaks if b.get("restore_sec", 0) > 0), None)
        if first:
            at = float(first.get("at_sec", 0) or 0)
            rst = float(first.get("restore_sec", 0) or 0)
            tl = (up or {}).get("timeline", [])
            if tl:
                result["recovery_sec"] = self._estimate_recovery_timeline(tl, at, rst)
            else:
                result["recovery_sec"] = self._estimate_recovery(monitor.samples, at, rst)
        self._log("info", f"[{group['name']}] 完成 上行={up_mbps}Mbps 下行={down_mbps}Mbps "
                          f"效率={result['efficiency_up_pct']}%/{result['efficiency_down_pct']}%")
        return result

    # ---------- 断链/恢复 ----------
    @staticmethod
    def _normalize_breaks(group: dict) -> list[dict]:
        """兼容旧单条 break，统一成列表 [{ifaces:[..], at_sec, restore_sec}]。"""
        out = []
        for b in (group.get("breaks") or []):
            ifaces = b.get("ifaces")
            if isinstance(ifaces, str):
                ifaces = [x.strip() for x in re.split(r"[,\s]+", ifaces) if x.strip()]
            if ifaces and float(b.get("at_sec", 0) or 0) > 0:
                out.append({"ifaces": ifaces, "at_sec": float(b.get("at_sec") or 0),
                            "restore_sec": float(b.get("restore_sec") or 0)})
        old = group.get("break") or {}
        if not out and old.get("enabled") and old.get("iface") and float(old.get("at_sec", 0) or 0) > 0:
            out.append({"ifaces": [old["iface"]], "at_sec": float(old.get("at_sec") or 0),
                        "restore_sec": float(old.get("restore_sec") or 0)})
        return out

    def _do_break_multi(self, b: dict):
        ifaces = b.get("ifaces") or []
        at = float(b.get("at_sec", 0) or 0)
        restore = float(b.get("restore_sec", 0) or 0)
        time.sleep(at)
        if self._stop.is_set():
            return
        self._log("warn", f"[断链] 第{at:.0f}s 断开 {', '.join(ifaces)}")
        for iface in ifaces:
            self._emit("break", action="down", iface=iface)
            try:
                self._ssh.run(f"ip link set {iface} down", timeout=10)
                if iface not in self._broken_ifaces:
                    self._broken_ifaces.append(iface)
            except Exception as exc:
                self._log("error", f"[断链] {iface} down 失败: {exc}")
        if restore > 0:
            time.sleep(restore)
            if self._stop.is_set():
                return
            self._log("warn", f"[断链] 恢复 {', '.join(ifaces)}")
            for iface in ifaces:
                self._emit("break", action="up", iface=iface)
                try:
                    self._ssh.run(f"ip link set {iface} up", timeout=10)
                    if iface in self._broken_ifaces:
                        self._broken_ifaces.remove(iface)
                except Exception as exc:
                    self._log("error", f"[断链] {iface} up 失败: {exc}")

    @staticmethod
    def _estimate_recovery(samples: list[dict], at_sec: float, restore_sec: float) -> float | None:
        """估算断链并恢复后，聚合吞吐回到断链前 85% 水平所需秒数。"""
        if not samples:
            return None
        t0 = samples[0]["t"]
        before = [s["tx_total"] for s in samples if s["t"] - t0 < at_sec - 1]
        if not before:
            return None
        base = sum(before) / len(before) * 0.85
        restore_t = t0 + at_sec + restore_sec
        for s in samples:
            if s["t"] >= restore_t and s["tx_total"] >= base:
                return round(s["t"] - restore_t, 1)
        return None

    @staticmethod
    def _estimate_recovery_timeline(timeline: list[dict], at_sec: float, restore_sec: float) -> float | None:
        """用 iperf 上行逐秒时间线估算断链恢复耗时（t 相对上行起点）。"""
        if not timeline:
            return None
        # 断链前基线：断链点前 1~2s 的平均
        before = [x["mbps"] for x in timeline if x["t"] <= at_sec - 1]
        if not before:
            return None
        base = (sum(before) / len(before)) * 0.85
        restore_t = at_sec + restore_sec
        for x in timeline:
            if x["t"] >= restore_t and x["mbps"] >= base:
                return round(x["t"] - restore_t, 1)
        return None
