<div align="center">
  <img src="docs/assets/candlemind-logo.png" alt="CandleMind Logo" width="180">
  <h1>CandleMind</h1>
  <p><strong>面向 Binance Futures 的开源趋势交易研究与自动化执行平台</strong></p>
  <p>
    <strong>简体中文</strong> |
    <a href="README_EN.md">English</a> |
    <a href="README_JA.md">日本語</a> |
    <a href="README_KO.md">한국어</a>
  </p>
  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f.svg" alt="MIT License"></a>
    <img src="https://img.shields.io/badge/Python-3.12-3776AB.svg" alt="Python 3.12">
    <img src="https://img.shields.io/badge/FastAPI-API-009688.svg" alt="FastAPI">
    <img src="https://img.shields.io/badge/React-18-61DAFB.svg" alt="React 18">
  </p>
</div>

> [!WARNING]
> CandleMind 仅用于技术研究与教育，不构成投资建议。自动化策略可以向 Binance Futures 测试网或真实网发送订单；测试网为默认环境，真实网默认由服务端禁用。历史研究、示例数字和回测结果不是事实业绩，也不代表未来收益。

## 项目简介

CandleMind 采用 FastAPI 与 React 构建，将实时行情、趋势分析、策略配置、交易所执行运行时和账户统计整合到一个工作台。平台聚焦 **CandleMind 趋势策略**，行情页提供铺满可用视口、支持拖拽调整的 K 线图与实时 AI 行情助手，并保留可复现的离线研究基础设施。

设置页提供全局市场选择器；目前仅 **Binance Futures** 已接入。OKX、Bybit、Gate.io 与 A 股是后续接入占位，选择后业务页面会明确显示未连接，且不会发起 Binance 行情、账户或交易请求。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 实时行情 | Binance WebSocket 行情、K 线、主图指标，以及自适应视口的可拖拽工作区 |
| AI 行情助手 | 基于已收盘多周期 K 线持续解读行情，并支持随时对话 |
| 策略运行时 | 三种可配置自动化策略，绑定所选品种与交易网络 |
| 订单与账户 | 挂单、成交、历史订单及收益、胜率、盈亏比统计 |
| 离线研究 | 数据校验、策略评估和强化学习研究契约 |
| 交易安全 | 测试网优先、真实网双重开关、数量校验与幂等订单日志 |

当前公开产品包含概览、行情、订单、策略和设置五个页面。内部评估能力不提供公开回测页面或 `/api/backtest/*` 接口。

设置页打开期间会立即检测出口 IP，并每分钟自动刷新一次；上一次结果会保留到下一次检测完成。全局交易所选择会同步作用于概览、行情和订单等业务页面；顶部栏可统一切换品种，并通过“刷新”入口更新当前页面数据。

## 技术架构

| 模块 | 技术 | 目录 |
| --- | --- | --- |
| 后端 API | Python 3.12、FastAPI、Pandas | `backend/app/` |
| 策略与评估 | 自动化策略运行时、Backtrader 离线评估 | `backend/app/strategies/` |
| 前端 | React 18、Vite、Tailwind CSS | `frontend/src/` |
| 部署 | Docker Compose、Nginx | `docker-compose.yml` |
| 外部数据 | K 线、运行状态与研究报告 | `G:/CandleMind/CandleMind_data` |

仓库不保存生产行情、数据库、密钥或生成报告。数据归属规则见 [`docs/DATA_LAYOUT.md`](docs/DATA_LAYOUT.md)。

## 快速开始

### Docker Compose

准备 Docker Desktop，然后执行：

```powershell
Copy-Item .env.example .env
powershell -ExecutionPolicy Bypass -File ops/dev-compose.ps1
```

服务启动后访问：

- Web：<http://localhost:3000>
- API：<http://localhost:8000>
- 健康检查：<http://localhost:8000/api/ping>

如需修改外部数据位置，在 `.env` 中设置：

```dotenv
CANDLEMIND_DATA_ROOT=D:/CandleMind/data
CANDLEMIND_RUNTIME_ROOT=D:/CandleMind/runtime/app
```

### 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt
python -m uvicorn backend.app.main:app --reload --env-file .env --port 8000
```

另开终端启动前端：

```powershell
cd frontend
npm ci
npm run dev
```

Vite 默认运行在 <http://localhost:5173>，并将 API 请求代理至后端。

## 配置与交易安全

1. 从 `.env.example` 创建 `.env`，不要提交密钥、数据库或运行日志。
2. 在设置页录入 Binance 和 AI Provider 凭据；备份时同时保存 `trader.db` 与 `secret.key`。
3. 设置页打开期间每分钟自动检测出口 IP；该结果仅用于连接诊断，不代替交易所 API 的权限和 IP 白名单配置。
4. 云端 AI Base URL 仅允许受信任的 HTTPS 主机；本地 Provider 可使用回环或 RFC1918 地址。详见 [`docs/AI_CONFIGURATION.md`](docs/AI_CONFIGURATION.md)。
5. 真实网交易必须先完成 testnet 验收，并同时启用服务端开关和页面确认。
6. 使用真实资金前，应独立审查策略、仓位、杠杆、止损和交易所权限。

Binance 读取重试、限流冷却、IP 判断和订单确认规则见 [`docs/BINANCE_RESILIENCE.md`](docs/BINANCE_RESILIENCE.md)。
交易所选择、持久化及未接入市场的隔离规则见 [`docs/EXCHANGE_PROVIDERS.md`](docs/EXCHANGE_PROVIDERS.md)。

## 强化学习研究

仓库保留基于 EMA 特征的强化学习趋势跟踪研究基础设施，包括特征工程、数据发布、生命周期和来源校验契约。这些模块仅用于离线研究与实验复现，**强化学习模型尚未接入在线推理、订单决策或实盘执行**。详细边界见 [`docs/research/RL_RESEARCH_STATUS.md`](docs/research/RL_RESEARCH_STATUS.md)。

## 测试与验证

```powershell
# 完整隔离验证
powershell -ExecutionPolicy Bypass -File ops/verify.ps1

# 分项验证
python -m pytest backend/tests -q
cd frontend
npm test
npm run build
```

验证过程使用临时数据目录，不会修改 G 盘生产数据。

## 项目结构

```text
CandleMind/
|-- backend/app/        # API、服务、策略与运行时
|-- backend/scripts/    # 数据维护与离线评估命令
|-- backend/tests/      # 单元、契约、安全与回归测试
|-- frontend/src/       # 页面、组件、状态与 API 客户端
|-- docs/               # 数据、研究、安全与运维文档
|-- ops/                # 部署和隔离验证脚本
`-- docker-compose.yml  # 容器编排
```

## 贡献

请先阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)、[`AGENTS.md`](AGENTS.md) 和 [`docs/README.md`](docs/README.md)。提交前运行完整验证；安全问题请按 [`SECURITY.md`](SECURITY.md) 私下报告，不要在公开 Issue 中披露密钥或漏洞细节。

## 特别致谢

<table>
  <tr>
    <td align="center" width="240">
      <a href="https://netapi.cc/"><img src="docs/assets/netapi-logo.png" alt="NetAPI Logo" width="210"></a>
    </td>
    <td>
      感谢 <a href="https://netapi.cc/"><strong>NetAPI.cc</strong></a> 为 CandleMind 提供 Token 支持。一个 API 密钥即可调用主流 AI 模型，支持智能调度与按量付费。<br><br>
      <strong>“就像一个万能充电头，什么手机都能充。”</strong>
    </td>
  </tr>
</table>

## 交流群

欢迎加入 AI 自动化交易交流群，讨论量化研究、工程实践与风险控制。

<p align="center">
  <img src="docs/assets/wechat-trading-community.jpg" alt="AI 自动化交易交流群二维码" width="360">
</p>

[二维码无法加载时通过 CDN 查看](https://testingcf.jsdelivr.net/gh/JacobeZhao/CandleMind@main/docs/assets/wechat-trading-community.jpg)

## 开源许可

CandleMind 基于 [MIT License](LICENSE) 开源。使用、修改或分发时请保留版权与许可声明；第三方依赖遵循各自许可证。
