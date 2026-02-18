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