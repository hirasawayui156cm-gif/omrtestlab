/* OMR 多链路聚合测试台 - 前端逻辑 */
"use strict";

const $ = (id) => document.getElementById(id);

// ---------------- 全局状态 ----------------
let CONFIG = null;
let GROUPS = [];
let interfacesCache = [];
let ws = null;
let liveChart = null;
let liveSeries = { t: [], up: [], down: [] };
let liveBase = null;
let runInProgress = false;

// ---------------- 工具 ----------------
async function api(url, opts = {}) {
  const o = { headers: { "Content-Type": "application/json" }, ...opts };
  const res = await fetch(url, o);
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json().catch(() => ({}));
  if (data && data.ok === false) throw new Error(data.error || "请求失败");
  return data;
}
function mb(x) { return (x == null ? 0 : x).toFixed(2); }
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ---------------- Tabs ----------------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tabpage").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "result") loadResults();
  });
});

// ---------------- 配置 ----------------
async function loadConfig() {
  try {
    CONFIG = await api("/api/config");
    const r = CONFIG.router || {}, v = CONFIG.vps || {}, i = CONFIG.iperf3 || {};
    $("r_host").value = r.host || ""; $("r_port").value = r.port || 22;
    $("r_user").value = r.user || "root"; $("r_pass").value = r.password || "";
    $("r_sudo").checked = !!r.use_sudo;
    $("v_host").value = v.host || ""; $("v_port").value = v.port || 22;
    $("v_user").value = v.user || "root"; $("v_pass").value = v.password || "";
    $("v_sudo").checked = !!v.use_sudo;
    $("i_port").value = i.port || 5201;
    $("i_mptcp").checked = i.mptcp !== false;
    GROUPS = (CONFIG.groups || []).slice();
    renderGroups();
    renderGroupSelector();
  } catch (e) { log("error", "加载配置失败: " + e.message); }
}

function collectConfigFromForm() {
  return {
    router: {
      host: $("r_host").value.trim(), port: parseInt($("r_port").value) || 22,
      user: $("r_user").value.trim(), password: $("r_pass").value,
      use_sudo: $("r_sudo").checked,
    },
    vps: {
      host: $("v_host").value.trim(), port: parseInt($("v_port").value) || 22,
      user: $("v_user").value.trim(), password: $("v_pass").value,
      use_sudo: $("v_sudo").checked,
    },
    iperf3: { port: parseInt($("i_port").value) || 5201, mptcp: $("i_mptcp").checked, udp_bitrate_mbps: 0 },
    groups: GROUPS,
  };
}

async function saveFormConfig() {
  const cfg = collectConfigFromForm();
  await api("/api/config", { method: "POST", body: JSON.stringify(cfg) });
  CONFIG = cfg;
}

async function checkHost(which) {
  const cfg = collectConfigFromForm();
  await api("/api/config", { method: "POST", body: JSON.stringify(cfg) });
  CONFIG = cfg;
  const btnText = $(which === "router" ? "r_host" : "v_host").closest(".card")
    ? null : null;
  try {
    const r = await api("/api/check/" + which, { method: "POST" });
    const el = $(which === "router" ? "r_ifaces" : "connState");
    log("info", `[${which}] ${r.message}`);
    if (el && el.id === "r_ifaces") {
      el.textContent = "SSH 连接成功 ✓  " + (r.error ? "警告: " + r.error : "");
      el.style.color = "var(--ok)";
    }
    return r;
  } catch (e) {
    const el = $(which === "router" ? "r_ifaces" : "connState");
    if (el && el.id === "r_ifaces") {
      el.textContent = "连接失败: " + e.message; el.style.color = "var(--err)";
    }
    log("error", `[${which}] ${e.message}`);
    return { ok: false, error: e.message };
  }
}

async function refreshInterfaces() {
  try {
    await saveFormConfig();
    const r = await api("/api/interfaces");
    if (r.ok) {
      interfacesCache = r.interfaces;
      $("r_ifaces").textContent = "检测到接口: " + r.interfaces.join(", ");
      $("r_ifaces").style.color = "var(--ok)";
      renderGroups();
    } else { $("r_ifaces").textContent = r.error; $("r_ifaces").style.color = "var(--err)"; }
  } catch (e) {
    $("r_ifaces").textContent = "接口检测失败: " + e.message;
    $("r_ifaces").style.color = "var(--err)";
  }
}

async function prepareEnv() {
  try {
    await saveFormConfig();
    const r = await api("/api/prepare", { method: "POST" });
    let s = "";
    for (const [k, v] of Object.entries(r)) {
      s += `[${k}] ` + (v.ok ? "✓ " + (v.message || "") : "✗ " + (v.error || "")) + "\n";
    }
    $("prepareOut").textContent = s;
    log("info", "环境准备完成: " + s.trim().replace(/\n/g, " | "));
  } catch (e) {
    $("prepareOut").textContent = "错误: " + e.message;
    log("error", "环境准备失败: " + e.message);
  }
}

// ---------------- 实验组编辑 ----------------
function ifaceOptions(selected) {
  const opts = interfacesCache.length
    ? interfacesCache.map((x) => `<option value="${esc(x)}" ${x === selected ? "selected" : ""}>${esc(x)}</option>`)
    : "";
  return `<option value="${esc(selected || "")}">${esc(selected || "选择接口")}</option>` + opts;
}

function renderGroups() {
  const box = $("groupsEditor");
  box.innerHTML = GROUPS.map((g, gi) => {
    const brk = g.break || {};
    return `
    <div class="group-editor" data-gi="${gi}">
      <div class="ge-head">
        <label>编号<input type="text" value="${esc(g.gid || "")}" style="width:70px" placeholder="编号" data-f="gid" data-gi="${gi}"></label>
        <input type="text" value="${esc(g.name)}" placeholder="实验组名称" data-f="name" data-gi="${gi}">
        <label>整形方式<select data-f="shaping" data-gi="${gi}" title="core:由CORE仿真整形，程序不打tc，限速仅作参考计算效率">
          <option value="program" ${g.shaping === "core" ? "" : "selected"}>程序 tc 整形</option>
          <option value="core" ${g.shaping === "core" ? "selected" : ""}>CORE 整形(不打tc)</option>
        </select></label>
        <label>时长(s)<input type="number" value="${g.duration || 30}" min="5" data-f="duration" data-gi="${gi}"></label>
        <label>协议<select data-f="protocol" data-gi="${gi}">
          <option value="tcp" ${g.protocol === "udp" ? "" : "selected"}>TCP（上行+下行）</option>
          <option value="udp" ${g.protocol === "udp" ? "selected" : ""}>UDP（含抖动/丢包统计）</option>
        </select></label>
        <label>UDP带宽(M) <input type="number" value="${g.udp_bitrate_mbps || 0}" placeholder="0=按理论值" data-f="udp_bitrate_mbps" data-gi="${gi}"></label>
        <button onclick="delGroup(${gi})" class="danger">删除组</button>
      </div>
      <span class="hint" id="shpHint_${gi}">${g.shaping === "core"
        ? "CORE整形模式：程序跳过 tc，限速/时延/丢包仅作参考用于计算效率（不会实际整形）"
        : ""}</span>
      <table class="ge-links">
        <thead><tr><th>启用</th><th>链路名</th><th>接口</th><th>限速 Mbps</th>
        <th>时延 ms</th><th>抖动 ms</th><th>丢包 %</th><th></th></tr></thead>
        <tbody>
          ${(g.links || []).map((l, li) => `
          <tr data-gi="${gi}" data-li="${li}">
            <td><input type="checkbox" ${l.enabled ? "checked" : ""} data-f="enabled"></td>
            <td><input type="text" value="${esc(l.label)}" style="width:80px" data-f="label"></td>
            <td><select data-f="iface">${ifaceOptions(l.iface)}</select></td>
            <td><input type="number" value="${l.rate_mbps || 0}" min="0" style="width:90px" data-f="rate_mbps"></td>
            <td><input type="number" value="${l.delay_ms || 0}" min="0" style="width:80px" data-f="delay_ms"></td>
            <td><input type="number" value="${l.jitter_ms || 0}" min="0" style="width:80px" data-f="jitter_ms"></td>
            <td><input type="number" value="${l.loss_pct || 0}" min="0" step="0.1" style="width:80px" data-f="loss_pct"></td>
            <td><button onclick="delLink(${gi},${li})" class="danger">×</button></td>
          </tr>`).join("")}
        </tbody>
      </table>
      <button onclick="addLink(${gi})">+ 添加链路</button>
      <div class="ge-break">
        <label><input type="checkbox" data-f="brk_enabled" data-gi="${gi}" ${brk.enabled ? "checked" : ""}> 断链/容灾测试</label>
        <label>断开接口 <select data-f="brk_iface" data-gi="${gi}">${ifaceOptions(brk.iface || (g.links && g.links[0] && g.links[0].iface))}</select></label>
        <label>断开时间点(s)<input type="number" value="${brk.at_sec || 0}" min="0" data-f="brk_at_sec" data-gi="${gi}"></label>
        <label>恢复间隔(s)<input type="number" value="${brk.restore_sec || 0}" min="0" data-f="brk_restore_sec" data-gi="${gi}"></label>
      </div>
    </div>`;
  }).join("") || '<p class="hint">尚未创建实验组，点击右上角“添加实验组”。</p>';
}

function addGroup() {
  GROUPS.push({
    name: "实验组 " + (GROUPS.length + 1),
    gid: String(GROUPS.length + 1),
    duration: 15, protocol: "tcp", udp_bitrate_mbps: 0, shaping: "program",
    links: [
      { label: "链路1", iface: interfacesCache[0] || "eth0", rate_mbps: 100, delay_ms: 10, jitter_ms: 0, loss_pct: 0, enabled: true },
      { label: "链路2", iface: interfacesCache[1] || "eth1", rate_mbps: 50, delay_ms: 20, jitter_ms: 0, loss_pct: 0, enabled: true },
    ],
    break: { enabled: false, iface: interfacesCache[1] || "eth1", at_sec: 0, restore_sec: 5 },
  });
  renderGroups(); renderGroupSelector();
}
function delGroup(gi) { GROUPS.splice(gi, 1); renderGroups(); renderGroupSelector(); }
function addLink(gi) {
  GROUPS[gi].links.push({ label: "链路" + (GROUPS[gi].links.length + 1), iface: "", rate_mbps: 20, delay_ms: 0, jitter_ms: 0, loss_pct: 0, enabled: true });
  renderGroups();
}
function delLink(gi, li) { GROUPS[gi].links.splice(li, 1); renderGroups(); }

// 输入绑定（事件委托）
$("groupsEditor").addEventListener("input", onGeInput);
$("groupsEditor").addEventListener("change", onGeInput);
function onGeInput(ev) {
  const el = ev.target;
  const f = el.dataset.f;
  if (!f) return;
  const row = el.closest("tr");
  const gi = Number(el.dataset.gi ?? row?.dataset.gi);
  if (isNaN(gi) || !GROUPS[gi]) return;
  if (f === "name") { GROUPS[gi].name = el.value; }
  else if (f === "gid") { GROUPS[gi].gid = el.value; }
  else if (f === "duration") GROUPS[gi].duration = +el.value;
  else if (f === "protocol") GROUPS[gi].protocol = el.value;
  else if (f === "shaping") {
    GROUPS[gi].shaping = el.value;
    const h = $("shpHint_" + gi);
    if (h) h.textContent = GROUPS[gi].shaping === "core"
      ? "CORE整形模式：程序跳过 tc，限速/时延/丢包仅作参考用于计算效率（不会实际整形）"
      : "";
  }
  else if (f === "udp_bitrate_mbps") GROUPS[gi].udp_bitrate_mbps = +el.value;
  else if (f === "brk_enabled") GROUPS[gi].break = GROUPS[gi].break || {}, GROUPS[gi].break.enabled = el.checked;
  else if (f === "brk_iface") GROUPS[gi].break = GROUPS[gi].break || {}, GROUPS[gi].break.iface = el.value;
  else if (f === "brk_at_sec") GROUPS[gi].break = GROUPS[gi].break || {}, GROUPS[gi].break.at_sec = +el.value;
  else if (f === "brk_restore_sec") GROUPS[gi].break = GROUPS[gi].break || {}, GROUPS[gi].break.restore_sec = +el.value;
  else {
    const li = Number(el.dataset.li ?? row?.dataset.li);
    if (!isNaN(li) && GROUPS[gi].links[li]) {
      const l = GROUPS[gi].links[li];
      if (f === "enabled") l.enabled = el.checked;
      else if (f === "label") l.label = el.value;
      else if (f === "iface") l.iface = el.value;
      else if (f === "rate_mbps") l.rate_mbps = +el.value;
      else if (f === "delay_ms") l.delay_ms = +el.value;
      else if (f === "jitter_ms") l.jitter_ms = +el.value;
      else if (f === "loss_pct") l.loss_pct = +el.value;
    }
  }
}

async function saveGroups() {
  const cfg = collectConfigFromForm();
  await api("/api/groups", { method: "POST", body: JSON.stringify({ groups: GROUPS }) });
  CONFIG = cfg;
  renderGroupSelector();
  log("info", "实验设计已保存，共 " + GROUPS.length + " 组");
}

// ---------------- 运行 ----------------
function renderGroupSelector() {
  $("groupSelector").innerHTML = GROUPS.map((g, i) => `
    <label class="chk" style="margin:6px 0">
      <input type="checkbox" value="${i}" checked>
      <span>${esc(g.name)}  [${(g.shaping === "core" ? "CORE整形" : "tc整形")} | ${g.protocol.toUpperCase()} ${g.duration}s] 参考理论 ${theory(g)}Mbps</span>
    </label>`).join("") || '<p class="hint">请先在“实验设计”中添加实验组。</p>';
}
function theory(g) {
  return (g.links || []).filter((l) => l.enabled).reduce((a, l) => a + (+l.rate_mbps || 0), 0);
}

async function startRuns() {
  const idx = [...document.querySelectorAll("#groupSelector input:checked")].map((c) => +c.value);
  if (!idx.length) { log("warn", "请至少勾选一个实验组"); return; }
  const groups = idx.map((i) => GROUPS[i]);
  try {
    await saveGroups();
    const r = await api("/api/runs/start", { method: "POST", body: JSON.stringify({ groups }) });
    if (!r.ok) { log("error", r.error); return; }
    runInProgress = true; setRunUI(true);
    liveSeries = { t: [], up: [], down: [] };
    resetLiveChart();
  } catch (e) { log("error", "启动失败: " + e.message); }
}

async function stopRuns() {
  await api("/api/runs/stop", { method: "POST" });
  log("warn", "正在停止…");
}

function setRunUI(running) {
  $("btnStart").disabled = running;
  $("btnStop").disabled = !running;
  const dot = $("runStatus"), txt = $("runStatusText");
  dot.className = "status-dot " + (running ? "running" : "idle");
  txt.textContent = running ? "测试运行中" : "空闲";
}

// ---------------- 日志 ----------------
function log(level, msg) {
  const box = $("logBox");
  const line = document.createElement("div");
  line.className = "log-" + level;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}
function clearLog() { $("logBox").innerHTML = ""; }

// ---------------- 实时图表 ----------------
function liveChartInit() {
  if (liveChart) { liveChart.dispose(); }
  liveChart = echarts.init($("liveChart"));
  liveChart.setOption({
    grid: { left: 55, right: 20, top: 30, bottom: 30 },
    legend: { data: ["上行(Mbps)", "下行(Mbps)"], textStyle: { color: "#9fb8d8" } },
    xAxis: { type: "category", data: [], name: "时间(s)", axisLabel: { color: "#8b9bb4" } },
    yAxis: { type: "value", name: "Mbps", axisLabel: { color: "#8b9bb4" } },
    series: [
      { name: "上行(Mbps)", type: "line", showSymbol: false, lineStyle: { color: "#60a5fa", width: 2 }, data: [] },
      { name: "下行(Mbps)", type: "line", showSymbol: false, lineStyle: { color: "#f97316", width: 2 }, data: [] },
    ],
  });
}
function resetLiveChart() {
  liveSeries = { t: [], up: [], down: [] };
  liveBase = null;
  if (liveChart) liveChart.setOption({ xAxis: { data: [] }, series: [{ data: [] }, { data: [] }] });
}
function pushLive(sample) {
  if (liveBase === null) liveBase = sample.t;
  const t = Math.round(sample.t - liveBase);
  liveSeries.t.push(t);
  liveSeries.up.push(+sample.tx_total.toFixed(2));
  liveSeries.down.push(+sample.rx_total.toFixed(2));
  if (liveSeries.t.length > 400) { liveSeries.t.shift(); liveSeries.up.shift(); liveSeries.down.shift(); }
  if (liveChart) {
    liveChart.setOption({
      xAxis: { data: liveSeries.t },
      series: [{ data: liveSeries.up }, { data: liveSeries.down }],
    });
  }
}
function ensureLiveChart() {
  if (window.echarts && !liveChart) liveChartInit();
}

// ---------------- WebSocket ----------------
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    let e; try { e = JSON.parse(ev.data); } catch (_) { return; }
    handleEvent(e);
  };
  ws.onclose = () => { setTimeout(connectWS, 2000); };
}
function handleEvent(e) {
  if (e.type === "log") log(e.level, e.msg);
  else if (e.type === "group_start") {
    log("info", `===== 实验组 [${e.name}] (${e.index + 1}/${e.total}) =====`);
    $("progText").textContent = `${e.index + 1} / ${e.total}`;
    $("progPhase").textContent = "开始 " + e.name;
    $("progressBarWrap").style.display = "block";
    resetLiveChart();
  } else if (e.type === "phase") {
    $("progPhase").textContent = (e.name === "up" ? "上行测速中…" : "下行测速中…") + ` ${e.mbps}Mbps`;
  } else if (e.type === "live") {
    ensureLiveChart(); pushLive(e.sample);
  } else if (e.type === "break") {
    log("warn", `[断链] ${e.action === "down" ? "断开" : "恢复"} ${e.iface}`);
    $("progPhase").textContent = `断链测试: ${e.action === "down" ? "断开 " : "恢复 "}${e.iface}`;
  } else if (e.type === "group_done") {
    log("info", `✅ 实验组完成: 上行=${e.result.upload_mbps}Mbps 下行=${e.result.download_mbps}Mbps 效率=${e.result.efficiency_up_pct}%/${e.result.efficiency_down_pct}%`);
  } else if (e.type === "all_done") {
    runInProgress = false; setRunUI(false);
    $("progressBarWrap").style.display = "none";
    log("info", "===== 全部测试完成 =====");
    loadResults();
  }
}

// ---------------- 结果分析 ----------------
let RUNS = [];
let selIds = new Set();

function visRuns() { return RUNS.filter((r) => selIds.has(r.id)); }
let charts = {};

function makeChart(id, w = 420, h = 280) {
  const el = $(id);
  if (!window.echarts) return null;
  const c = charts[id] || (charts[id] = echarts.init(el, null, { width: Math.min(w, el.clientWidth || w), height: h }));
  return c;
}
function chartOpt(base) {
  return Object.assign({
    grid: { left: 55, right: 30, top: 40, bottom: 30 },
    legend: { textStyle: { color: "#9fb8d8" }, top: 5 },
    tooltip: { trigger: "axis", backgroundColor: "#1a2233", borderColor: "#2c3a55", textStyle: { color: "#dbe4f0" } },
  }, base);
}

async function persistRunOrder() {
  try {
    await api("/api/order", { method: "POST", body: JSON.stringify({ ids: RUNS.map((r) => r.id) }) });
  } catch (e) { log("error", "保存排序失败: " + e.message); }
}

async function loadResults() {
  try {
    const r = await api("/api/runs");
    RUNS = r.runs || [];
    const ids = new Set(RUNS.map((x) => x.id));
    selIds = new Set([...selIds].filter((id) => ids.has(id)));
    if (RUNS.length && selIds.size === 0) selIds = new Set(ids);
    renderSummaryCharts();
    renderRunsTable();
  } catch (e) { log("error", "加载结果失败: " + e.message); }
}

function groupNo(r) { return r.gid != null && r.gid !== "" ? String(r.gid) : String(r.id); }
function runLabels(list) { return list.map((r) => groupNo(r)); }

function renderSummaryCharts() {
  if (!window.echarts) { alert("图表库未加载，请检查网络"); return; }
  const R = visRuns();
  if (!R.length) { clearCharts(); return; }

  const labels = runLabels(R);
  // 1) 吞吐对比
  makeChart("chartThru")?.setOption(chartOpt({
    title: { text: "", textStyle: { color: "#9fc3ff", fontSize: 13 } },
    tooltip: { trigger: "axis" },
    legend: { data: ["上行", "下行"] },
    xAxis: { type: "category", data: labels, axisLabel: { color: "#8b9bb4", rotate: 20 } },
    yAxis: { type: "value", name: "Mbps", axisLabel: { color: "#8b9bb4" } },
    series: [
      { name: "上行", type: "bar", data: R.map((r) => +r.upload_mbps.toFixed(2)), itemStyle: { color: "#60a5fa" }, barGap: "10%" },
      { name: "下行", type: "bar", data: R.map((r) => +r.download_mbps.toFixed(2)), itemStyle: { color: "#f97316" } },
    ],
  }));

  // 2) 效率对比
  makeChart("chartEff")?.setOption(chartOpt({
    legend: { data: ["上行效率%", "下行效率%"] },
    xAxis: { type: "category", data: labels, axisLabel: { color: "#8b9bb4", rotate: 20 } },
    yAxis: { type: "value", max: 100, name: "%", axisLabel: { color: "#8b9bb4" } },
    series: [
      { name: "上行效率%", type: "bar", data: R.map((r) => +r.efficiency_up_pct), itemStyle: { color: "#22c55e" } },
      { name: "下行效率%", type: "bar", data: R.map((r) => +r.efficiency_down_pct), itemStyle: { color: "#eab308" } },
    ],
  }));

  // 3) 每链路负载分布（tx/rx 峰值堆叠）
  const ifacesAll = [...new Set(R.flatMap((r) => Object.keys(r.per_link || {})))];
  const txS = ifacesAll.map((iface) => ({
    name: iface + " 上行", type: "bar", stack: "tx",
    data: R.map((r) => +((r.per_link[iface] || {}).tx_max_mbps || 0).toFixed(2)),
  }));
  const rxS = ifacesAll.map((iface) => ({
    name: iface + " 下行", type: "bar", stack: "rx",
    data: R.map((r) => +((r.per_link[iface] || {}).rx_max_mbps || 0).toFixed(2)),
  }));
  makeChart("chartLinks")?.setOption(chartOpt({
    legend: { data: [...ifacesAll.map((f) => f + " 上行"), ...ifacesAll.map((f) => f + " 下行")], type: "scroll" },
    xAxis: { type: "category", data: labels, axisLabel: { color: "#8b9bb4", rotate: 20 } },
    yAxis: { type: "value", name: "Mbps", axisLabel: { color: "#8b9bb4" } },
    series: [...txS, ...rxS],
  }));

  // 4) 各链路时延
  const rttS = ifacesAll.map((iface) => ({
    name: iface, type: "bar",
    data: R.map((r) => {
      const v = (r.per_link[iface] || {}).rtt || {};
      return v.avg_ms == null ? null : +v.avg_ms.toFixed(1);
    }),
  }));
  makeChart("chartRtt")?.setOption(chartOpt({
    legend: { data: ifacesAll },
    xAxis: { type: "category", data: labels, axisLabel: { color: "#8b9bb4", rotate: 20 } },
    yAxis: { type: "value", name: "ms", axisLabel: { color: "#8b9bb4" } },
    series: rttS,
  }));
}

function clearCharts() {
  ["chartThru", "chartEff", "chartLinks", "chartRtt"].forEach((id) => {
    if (charts[id]) { charts[id].clear(); }
  });
}

function renderRunsTable() {
  const tb = $("runsTable").querySelector("tbody");
  tb.innerHTML = RUNS.map((r, i) => `
    <tr class="clickable" draggable="true" data-idx="${i}" onclick="showDetail(${i})">
      <td><input type="checkbox" class="rowsel" data-id="${r.id}" ${selIds.has(r.id) ? "checked" : ""} onclick="event.stopPropagation()"></td>
      <td style="cursor:grab">${groupNo(r)}</td><td>${esc(r.ts)}</td><td title="${esc(r.group_name)}">${esc((r.group_name || "").slice(0, 22))}</td>
      <td>${r.shaping === "core" ? "CORE" : "程序tc"}</td>
      <td>${(r.protocol || "tcp").toUpperCase()}</td>
      <td>${r.theory_mbps}</td>
      <td class="up">${mb(r.upload_mbps)}</td><td class="dn">${mb(r.download_mbps)}</td>
      <td>${r.efficiency_up_pct}</td><td>${r.efficiency_down_pct}</td>
      <td>${r.retransmits}</td><td>${r.jitter_ms}</td><td>${r.lost_pct}</td>
      <td>${r.recovery_sec == null ? "-" : r.recovery_sec + "s"}</td>
      <td><button class="danger" style="padding:2px 8px" onclick="event.stopPropagation();deleteRun(${r.id},${i})">删除</button></td>
    </tr>`).join("") || '<tr><td colspan="16" class="hint">暂无结果</td></tr>';
  const all = tb.querySelectorAll(".rowsel");
  const chk = $("selAll");
  if (chk) { chk.checked = all.length > 0 && all.length === [...all].filter((c) => c.checked).length; chk.indeterminate = all.length > 0 && chk.checked === false && [...all].some((c) => c.checked); }
}

function toggleSel(id, on) {
  if (on) selIds.add(id); else selIds.delete(id);
  renderSummaryCharts();
  renderRunsTable();
}
function selectAllRows(on) {
  RUNS.forEach((r) => { on ? selIds.add(r.id) : selIds.delete(r.id); });
  renderSummaryCharts();
  renderRunsTable();
}

let currentDetail = null;
async function showDetail(i) {
  const r = RUNS[i]; if (!r) return;
  currentDetail = r;
  $("detailCard").style.display = "block";
  $("detailTitle").textContent = `明细 #${groupNo(r)} ${r.group_name}  ${r.ts}`;
  renderDetailCharts(r);
  renderDetailLinks(r);
  renderDetailBreak(r);
  $("detailCard").scrollIntoView({ behavior: "smooth" });
}
function closeDetail() { $("detailCard").style.display = "none"; currentDetail = null; }

function renderDetailCharts(r) {
  if (!window.echarts) return;
  // iperf3 时间线
  const upT = (r.timeline_up || []).map((x) => [x.t, x.mbps]);
  const dnT = (r.timeline_down || []).map((x) => [x.t, x.mbps]);
  makeChart("detailTimeline", 460, 280)?.setOption(chartOpt({
    legend: { data: ["上行", "下行"] },
    xAxis: { type: "category", data: [...new Set([...upT.map((x) => x[0]), ...dnT.map((x) => x[0])])], axisLabel: { color: "#8b9bb4" } },
    yAxis: { type: "value", name: "Mbps", axisLabel: { color: "#8b9bb4" } },
    series: [
      { name: "上行", type: "line", showSymbol: false, smooth: true, data: upT, lineStyle: { color: "#60a5fa", width: 2 } },
      { name: "下行", type: "line", showSymbol: false, smooth: true, data: dnT, lineStyle: { color: "#f97316", width: 2 } },
    ],
  }));

  // 监控时间线（各链路）
  const ifaces = Object.keys((r.per_link) || {});
  const mono = r.monitor || [];
  const cats = mono.map((m) => Math.round(m.t - (mono[0] ? mono[0].t : 0)));
  const txS = ifaces.map((f) => ({ name: f + " tx", type: "line", showSymbol: false, data: mono.map((m) => +((m.tx || {})[f] || 0).toFixed(2)) }));
  const rxS = ifaces.map((f) => ({ name: f + " rx", type: "line", showSymbol: false, data: mono.map((m) => +((m.rx || {})[f] || 0).toFixed(2)) }));
  makeChart("detailMonitor", 460, 280)?.setOption(chartOpt({
    legend: { data: [...txS, ...rxS].map((s) => s.name), type: "scroll" },
    xAxis: { type: "category", data: cats, axisLabel: { color: "#8b9bb4" } },
    yAxis: { type: "value", name: "Mbps", axisLabel: { color: "#8b9bb4" } },
    series: [...txS, ...rxS],
  }));
}

function renderDetailLinks(r) {
  const rows = (r.links || []).map((l) => {
    const pl = (r.per_link || {})[l.iface] || {};
    const rtt = pl.rtt || {};
    return `<tr>
      <td>${esc(l.label || l.iface)}</td><td>${esc(l.iface)}</td>
      <td>${l.rate_mbps || 0}M / ${l.delay_ms || 0}ms / ${l.loss_pct || 0}%</td>
      <td>${mb(pl.tx_avg_mbps)} / ${mb(pl.tx_max_mbps)}</td>
      <td>${mb(pl.rx_avg_mbps)} / ${mb(pl.rx_max_mbps)}</td>
      <td>${rtt.avg_ms == null ? "N/A" : rtt.avg_ms + "ms"}</td>
    </tr>`;
  }).join("");
  $("detailLinksTable").innerHTML = `<table class="runs"><thead><tr>
    <th>链路</th><th>接口</th><th>配置 速率/时延/丢包</th><th>上行 均/峰</th><th>下行 均/峰</th><th>实测时延</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

function renderDetailBreak(r) {
  const b = r.break || {};
  const lines = [];
  if (b && b.enabled) {
    lines.push(`断链接口: ${b.iface || "-"}  时间点: ${b.at_sec || 0}s  恢复间隔: ${b.restore_sec || 0}s`);
    lines.push(`断链恢复耗时(吞吐回到断链前85%): ${r.recovery_sec == null ? "未检测到/未启用" : r.recovery_sec + "s"}`);
  } else lines.push("本组未启用断链/容灾测试。");
  $("detailBreak").textContent = lines.join("\n");
}

function selIdsParam() {
  const ids = RUNS.filter((r) => selIds.has(r.id)).map((r) => r.id);
  if (!ids.length) return "";
  return "&ids=" + ids.join(",");
}
async function exportCsv() { window.open("/api/export?fmt=csv" + selIdsParam()); }
async function exportJson() { window.open("/api/export?fmt=json" + selIdsParam()); }
async function exportCharts(fmt) { window.open("/api/export-charts?fmt=" + fmt + selIdsParam()); }
async function deleteRun(id, i) {
  if (!confirm(`确定删除这条结果 #${id}？\n${RUNS[i] ? RUNS[i].group_name : ""}`)) return;
  try {
    await api("/api/runs/" + id, { method: "DELETE" });
    if (currentDetail && currentDetail.id === id) closeDetail();
    await loadResults();
    log("info", `已删除结果 #${id}`);
  } catch (e) { log("error", "删除失败: " + e.message); }
}
async function deleteRuns() {
  if (!confirm("确定清空所有结果？")) return;
  await api("/api/runs", { method: "DELETE" });
  RUNS = []; renderSummaryCharts(); renderRunsTable();
}

// ---------------- 启动 ----------------
window.addEventListener("resize", () => {
  Object.values(charts).forEach((c) => c && c.resize());
  if (liveChart) liveChart.resize();
});
loadConfig();
connectWS();
$("runsTable").addEventListener("change", (ev) => {
  if (ev.target.classList && ev.target.classList.contains("rowsel")) {
    toggleSel(+ev.target.dataset.id, ev.target.checked);
  }
});
// 拖拽调整明细行顺序（只影响展示/图表/导出顺序，不改数据）
let dragRowIdx = null;
$("runsTable").addEventListener("dragstart", (ev) => {
  const tr = ev.target.closest ? ev.target.closest("tr[data-idx]") : null;
  if (!tr) return;
  dragRowIdx = +tr.dataset.idx;
  ev.dataTransfer.effectAllowed = "move";
});
$("runsTable").addEventListener("dragover", (ev) => {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "move";
});
$("runsTable").addEventListener("drop", (ev) => {
  const tr = ev.target.closest ? ev.target.closest("tr[data-idx]") : null;
  if (!tr || dragRowIdx == null) { dragRowIdx = null; return; }
  const to = +tr.dataset.idx;
  if (dragRowIdx === to) { dragRowIdx = null; return; }
  const [moved] = RUNS.splice(dragRowIdx, 1);
  RUNS.splice(to, 0, moved);
  dragRowIdx = null;
  renderRunsTable();
  renderSummaryCharts();
  persistRunOrder();
});
