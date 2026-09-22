# OMR 多链路聚合实验台

用于 OpenMPTCProuter 三链路聚合实验的本地 Web 测试工具。

当前实验拓扑是：PVE 中运行 OMR 客户端，Debian 镜像运行 OMR VPS 服务端，三条 WAN 链路连接两端。工具通过 SSH 控制测试端，通过 `tc` 或 CORE 设置链路参数，使用 `iperf3` 测量吞吐，并保存结果用于对比分析。

## 功能

- OMR 客户端、Debian VPS、CORE 整形网关的环境配置
- 多实验组和三链路参数设计
- 程序 `tc`、CORE 场景和 CORE 实时整形模式
- TCP/UDP `iperf3` 测试、实时吞吐监控和 RTT 测量
- 断链/恢复测试、结果图表、CSV/JSON 导出
- 浅色/深色主题和离线调试模式

## 本地运行

需要 Python 3.11+：

```powershell
python -m pip install -r requirements.txt
python run.py --host 0.0.0.0 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。

首次使用时，复制 `data/settings.example.json` 为 `data/settings.json`，再在页面填写实际设备配置。`data/settings.json` 只用于本机，不要提交到公共仓库。

## 安全说明

请使用 SSH 密钥或仅在本地输入测试凭据。项目不会把已保存的密码返回给浏览器，但本地配置文件仍然包含运行所需的凭据，因此已加入 Git 忽略规则。

更多拓扑、CORE/lagsim 部署和测试流程见 [使用说明.md](使用说明.md)。

## License

GPL-3.0，见 [LICENSE](LICENSE)。
