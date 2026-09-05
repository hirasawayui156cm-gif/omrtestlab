# CORE(建网) + lagsim(弱网仿真) 组合部署清单

> 目标：用 **CORE 只负责建立三条"假ISP"链路拓扑**（不设置带宽/时延/丢包），
> 用 **lagsim 在同一台 Linux(电脑C) 上按各 WAN 源IP 施加带宽/时延/抖动/丢包等参数**，
> omrtestlab 保持"外部整形(CORE整形)"模式只做测速与结果分析。

---

## 0. 角色分工（先记清）

| 组件 | 职责 | 部署位置 |
|---|---|---|
| BPI-R4 (OMR 客户端) | 被测聚合路由器，三条上行 lan1/lan2/lan3 | 真实设备 |
| CORE | 只搭网：BPI 三条 WAN ↔ VPS 的三条"假ISP"路径；**不整形** | 电脑C(Linux) |
| lagsim | 施加参数：对每条 WAN(按源IP)上 带宽/时延/抖动/丢包，支持上下行不对称 | 电脑C(Linux)，作用在宿主接口层 |
| Debian VM (VPS) | OMR 服务端 / iperf3 对端 | 电脑C 上/其可达网段 |
| omrtestlab | 自动化测速/监控/画图 | 能 SSH BPI 与 VPS 的任意机器 |

**避免双重整形的总原则**：CORE 链路不设参数；只有 lagsim 在"变坏"。
用 `tc qdisc show` 检查——CORE 内 veth 无线速/时延规则，宿主侧才有 lagsim 规则。

---

## 1. 前置条件

- [ ] 电脑C 是 Linux（能跑 CORE；lagsim 需要 `tc`、`netem`、`ifb` 模块）
- [ ] BPI 三条上行（lan1/lan2/lan3）**物理/VLAN 分离地**进入电脑C，且源IP 可区分：
      lan1≈192.168.11.1、lan2≈192.168.20.1、lan3≈192.168.30.1（以你 `ip addr` 实测为准）
- [ ] BPI↔VPS 三条路在"不打参数"时都能通（各 WAN 单独 ping VPS 通）
- [ ] omrtestlab 能 SSH：路由器(BPI)、VPS
- [ ] BPI 防火墙对实验端口放行或实验期间停用（`/etc/init.d/firewall stop`）
- [ ] VPS 的 iperf3 服务端稳定运行（用 `setsid nohup iperf3 -s -p 5201 ... &` 方式）

---

## 2. 接线/拓扑（电脑C 上）

```
BPI lan1(11.1) ──vlan──► 电脑C 网卡 ethX.11（宿主接口）┐
BPI lan2(20.1) ──vlan──► 电脑C 网卡 ethX.20           ├─► lagsim 在此层按源IP上规则
BPI lan3(30.1) ──vlan──► 电脑C 网卡 ethX.30           ┘        │ 转发
                                                              ▼
                                                  CORE(只搭网，链路不整形)
                                                              │
                                                      Debian VM(VPS)
```

要点：
- BPI 的流量先到电脑C 的**宿主接口/vlan/ifb** → lagsim 能拦 → 再进 CORE。
- 确保三条流量不是"直接绕进 CORE 命名空间而宿主层看不见"。

---

## 3. CORE：只建链路，不整形

1. 在 CORE GUI 里照常建节点：GW-A / GW-B / GW-C（对应三条假ISP）+ 骨干节点。
2. 把 BPI 三条上行分别桥进 GW-A/B/C（同以前）。
3. **链路参数设置（关键）**：
   - 双击每条链路，把 **Bandwidth / Delay / Jitter / Loss / Duplicate / Corruption 全部留空或设为"不限/0"**；
   - 不要使用 CORE 的链路整形，不要给它填速率上限。
4. 保存并 **Start** 场景，让三条路先连通（此阶段应为"满速直连"）。

验证（此步先无 lagsim）：
```sh
# BPI 上三条路单独 ping VPS，时延都应是低基础值(~0-1ms 本地)
ping -I lan1 192.168.10.172
ping -I lan2 192.168.10.172
ping -I lan3 192.168.10.172
# 电脑C：确认 CORE 命名空间内 veth 上没有限速规则（无线速/时延即合格）
```

---

## 4. 安装 lagsim

```sh
# 电脑C 上（需要 Go）
git clone https://github.com/rs/lagsim
cd lagsim
go build -o lagsim .
sudo cp lagsim /usr/local/bin/
# 内核模块
sudo modprobe ifb
sudo modprobe sch_netem
# 开机转发
sudo sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf
```

---

## 5. 部署：让 lagsim 作用于正确的接口

lagsim 按"客户端 IP + 出口/入向接口"上规则。它默认作用于**能路由到该客户端 IP 的宿主接口**；如有多个，需指明接口。

1. 先看 lagsim 能列出哪些客户端：
```sh
sudo lagsim list          # 应能看到 11.1 / 20.1 / 30.1 等 BPI 的 WAN 源IP（或它们的网段网关）
sudo lagsim profiles      # 查看内置模板（3G/5G/Starlink/卫星/…）
```
2. 若没识别到客户端，检查 BPI 各 WAN 到电脑C 的路由/ARP 是否正常（`ip neigh`）。
3. 记住 BPI 三条源IP 与"该流量进电脑C 的接口"的对应关系，之后每条规则都指定到该接口方向。

---

## 6. 为三条 WAN 分别上规则（核心步骤）

以 lan3(30.1) 上 3G 模板为例：
```sh
sudo lagsim apply 192.168.30.1 3G
# 必要时指定接口：sudo lagsim apply 192.168.30.1 3G --iface <ethX.30>（按版本参数确认）
```
对三条分别：
```sh
sudo lagsim apply 192.168.11.1 <模板A>   # 链路1
sudo lagsim apply 192.168.20.1 <模板B>   # 链路2
sudo lagsim apply 192.168.30.1 <模板C>   # 链路3
```
> 需要自定义参数时：编辑 lagsim 的自定义方案文件（支持持久化），例如做"40M/80M/50M + 5/10/5ms + 抖动/丢包"，存成模板后一条命令套用。

管理：
```sh
sudo lagsim list            # 查看已生效规则
sudo lagsim remove 192.168.30.1   # 撤销单条
sudo lagsim teardown        # 全部清空（每组实验结束/开始前调用）
```

---

## 7. 验证命令（每换一组参数必做）

```sh
# 1) 规则是否命中：BPI 上该路 ping，时延应与模板一致（增大即生效）
ping -I lan3 192.168.10.172
# 2) 三条互不影响：只有 lan3 上模板时，lan1/lan2 ping 应保持低时延
ping -I lan1 192.168.10.172
# 3) 带宽生效：单条压到很小(如模板限20M)，从 BPI 该路单独 iperf，应≈模板值
iperf3 --mptcp -B 192.168.30.1 -c 192.168.10.172 -t 5
# 4) 无双重整形：电脑C 查 qdisc，CORE 内 veth 无线速，宿主侧 ifb/网卡才有
sudo tc qdisc show
# 5) 丢包/抖动可用 UDP 反向质检（VPS→BPI 方向），见 omrtestlab 说明
```

---

## 8. 与 omrtestlab 对接

1. 环境配置：路由器=BPI SSH、VPS=Debian SSH 填好并"一键准备"通过。
2. 实验组：
   - 整形方式选 **CORE整形(不打tc)**；
   - 每条链路填 **lagsim 模板实际参数**（如 40/80/50M、5/10/5ms）作为**参考**（算理论/效率用），程序不会真的打 tc；
   - 时长/协议按实验设计。
3. 每组运行前：在电脑C 用 `lagsim apply` 设好对应三条模板；跑完 `lagsim teardown`。
   （以后可加"lagsim 模式"让程序自动 apply/teardown。）
4. 结果页照常用；若每组重复3次取均值更稳。

---

## 9. 常见坑

| 现象 | 处理 |
|---|---|
| 三条仍共享同一出口，聚合上不去 | 先保 BPI 三条在进 CORE 前物理/VLAN 独立，别挤在同一真实链路 |
| lagsim list 看不到 BPI | 检查 BPI→电脑C 路由/ARP；BPI 各 WAN 默认网关指向电脑C |
| 时延生效但带宽没生效 | ifb 方向问题/接口没选对；确认 `--iface` 指向流量进入的宿主接口 |
| 与 CORE 双重整形 | CORE 链路把 带宽/时延 都留空；`tc qdisc show` 核对归属 |
| UDP 测不出/0 | 检查路径 UDP 放行（VPS、BPI 防火墙）；可改用 VPS→BPI 反向 UDP 质检 |
| 实验数值波动大 | 每组重复 2~3 次取均值；测前 `lagsim teardown` 清残留规则 |
