# IBX (Interactive Brokers Execution Engine)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/Platform-Mac%20%7C%20Synology%20%7C%20AWS-lightgrey.svg)](#)

**IBX** 是一个专为盈透证券（Interactive Brokers）打造的轻量级程序化交易执行引擎。它通过 **IB Gateway** 实现策略指令的自动化落地，旨在为开发者提供一个安全、稳定且易于扩展的交易底座。

---

## 🌟 核心特性

- **跨平台适配**：针对 macOS 本地开发、Synology NAS 长期运行以及 AWS 云端部署进行了优化。
- **安全隔离**：严格遵循网络安全规范，通过环境变量（`.env`）管理敏感凭据，确保账号安全。
- **异步驱动**：基于 `ib_insync` 构建，支持异步非阻塞的 API 调用，提升高频/多路交易的响应速度。
- **模块化架构**：将交易策略逻辑与底层执行逻辑解耦，支持快速接入自定义算法。

---

## 🏗 项目架构



```mermaid
graph LR
    A[Strategy Logic] --> B{IBX Engine}
    B --> C[IB Gateway / Docker]
    C --> D[IBKR Server]
    D --> E((Global Markets))
```

## ✅ 网关健康检查

启动网关后，可用以下命令快速验证网关是否正常：

```bash
make check
```

该检查会执行两步：

- TCP 连通性检查（默认端口 `4002,4001`）
- IB API 最小握手检查（默认端口 `4002`）

也可以直接运行脚本并自定义参数：

```bash
python3 scripts/check_ib_gateway.py --host 127.0.0.1 --ports 4002,4001 --api-port 4002
```

如果只想看端口是否打开（跳过 API 握手）：

```bash
python3 scripts/check_ib_gateway.py --skip-api
```

## 📊 查看当前资产组合

在网关正常可用后，执行：

```bash
make portfolio
```

或直接运行：

```bash
python3 scripts/list_portfolio.py --host 127.0.0.1 --port 4002 --client-id 99
```

常用参数：

- `--json`：以 JSON 输出，便于接入自动化流程
- `--account <账户号>`：只查看指定账户持仓
- `--port 4001`：查看实盘账户（`TRADING_MODE=live`）
