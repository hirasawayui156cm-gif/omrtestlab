# OMR 多链路聚合实验台

用于 OpenMPTCProuter（OMR）三链路聚合实验的本地 Web 测试工具。项目面向当前实验拓扑：一台软路由的 PVE 中运行 OMR 客户端，另一台软路由的 Debian 镜像运行 OMR VPS 服务端，三条 WAN 链路连接两端。

## 项目定位

实验台运行在一台可以访问 OMR 客户端和 VPS 的管理电脑上，通过 SSH 执行准备、测速和监控任务，并把实验结果保存到本地 SQLite 数据库。没有接通真实设备时，也可以先启动网页进行界面和参数流程调试；未配置的设备不会被自动连接。

```text
三条 WAN 链路
      │
      ▼
PVE 软路由 ── OMR 客户端/被测端
      │       ▲
      │       │ OMR 聚合隧道
      ▼       │
Debian 软路由 ── OMR VPS 服务端/iperf3 对端
```

## 功能

- OMR 客户端、Debian VPS 和整形主机的 SSH 配置
- 三条 WAN 链路的带宽、时延、抖动、丢包和恢复参数设计
- `tc`、CORE 场景和 CORE 实时整形模式
- TCP/UDP `iperf3` 测试、实时吞吐监控和 RTT 测量
- 断链/恢复测试，以及实验组的启动、停止和清理
- 结果图表、CSV/JSON 导出和 SQLite 本地存储
- 浏览器浅色/深色主题与离线调试界面

## 本地运行

需要 Python 3.11 或更高版本。首次安装依赖并启动服务：

```powershell
cd D:\Multi-link\omrtestlab
python -m pip install -r requirements.txt
python run.py --host 0.0.0.0 --port 8000
```

浏览器访问：<http://127.0.0.1:8000>

`run.py` 会在端口被占用时尝试下一个端口，并在终端输出实际访问地址。当前项目是 FastAPI 服务，必须先运行 Python 服务才能访问网页；没有真实设备时也可以直接运行服务进行离线调试。

## 配置

复制示例配置后再填写实际设备信息：

```powershell
Copy-Item data\settings.example.json data\settings.json
```

配置文件包含路由器、VPS、整形主机、`iperf3`、CORE 实时整形和实验组设置。`data/settings.json` 可能包含本地 SSH 凭据，只保留在本机，不要提交到公共仓库。建议优先使用 SSH 密钥，实验结束后轮换测试凭据。

没有接通设备时，可以先保留空的 `host` 字段，只检查页面、实验参数和导出流程；点击设备检查或启动测试前，再补充可达地址和凭据。

## 真实设备实验流程

1. 确认 PVE 中的 OMR 客户端和 Debian VPS 已启动。
2. 确认三条 WAN 链路分别可达，并确认 OMR 隧道状态正常。
3. 在 `data/settings.json` 或页面中填写 SSH 和 `iperf3` 配置。
4. 使用“准备环境”检查 `tc`、`iperf3` 和远端接口。
5. 创建实验组，设置三条链路参数和 TCP/UDP 测试选项。
6. 启动实验，观察实时吞吐、RTT、日志和链路状态。
7. 导出 CSV/JSON，并在需要时执行清理或断链恢复测试。

使用 CORE 与 lagsim 的组合部署时，请先阅读 [CORE-lagsim 组合部署清单](CORE-lagsim组合部署清单.md)。完整的设备接线、OMR 环境和测试步骤见 [使用说明](使用说明.md)。拓扑演示材料见 [omr.pptx](omr.pptx)。

## 目录结构

```text
omrtestlab/
├── run.py                 # FastAPI 启动入口
├── requirements.txt       # Python 依赖
├── app/
│   ├── server.py          # REST API、WebSocket 和静态页面
│   ├── runner.py          # 实验编排与后台运行器
│   ├── ssh.py             # Paramiko SSH 封装
│   ├── tc.py              # tc 整形命令生成
│   ├── iperf.py           # iperf3 执行与结果解析
│   ├── monitor.py         # 实时吞吐和 RTT 监控
│   ├── config.py          # 配置读写
│   ├── storage.py         # SQLite 结果存储
│   └── static/            # 网页、脚本和样式
└── data/
    ├── settings.example.json  # 配置模板
    ├── settings.json          # 本机配置，不应提交
    └── results.db             # 本地实验结果
```

## 安全与边界

- 不要把真实密码、私钥、`data/settings.json`、实验数据库或日志上传到公共仓库。
- 远端命令会改变接口整形、`iperf3` 服务和实验状态，只在隔离实验环境执行。
- 当前项目不负责创建 OMR 隧道或替代 OMR/VPS 本身；它负责实验编排、测试和结果记录。

## License

本项目采用 GPL-3.0，详见 [LICENSE](LICENSE)。
