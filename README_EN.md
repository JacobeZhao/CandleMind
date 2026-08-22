<div align="center">
  <img src="docs/assets/candlemind-logo.png" alt="CandleMind Logo" width="180">
  <h1>CandleMind</h1>
  <p><strong>Open-source trend trading research and automated execution for Binance Futures</strong></p>
  <p>
    <a href="README.md">简体中文</a> |
    <strong>English</strong> |
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
> CandleMind is provided for technical research and education only. It is not investment advice. Automated strategies can submit orders to Binance Futures testnet or mainnet; testnet is the default, while mainnet is disabled server-side by default. Historical research, examples, and backtests are neither actual performance nor guarantees of future returns.

## Overview

CandleMind combines live market data, trend analysis, strategy configuration, exchange execution, and account analytics in a FastAPI and React workspace. It focuses on the **CandleMind Trend Strategy**. The Markets page pairs a viewport-filling, resizable candlestick chart with a live AI market assistant, while reproducible infrastructure supports offline research.

Settings provides a global exchange selector. Only **Binance Futures** is implemented today. OKX, Bybit, Gate.io, and A-share are disconnected placeholders for future integrations; selecting one does not issue Binance market, account, or trading requests.

## Features

| Capability | Description |
| --- | --- |
| Live markets | Binance WebSocket quotes, candlesticks, overlays, and a viewport-responsive resizable workspace |
| AI market assistant | Ongoing analysis of closed multi-timeframe candles with interactive chat |
| Strategy runtime | Three configurable automated strategies bound to the selected symbol and network |
| Orders and account | Open orders, trades, order history, returns, win rate, and profit factor |
| Offline research | Data validation, strategy evaluation, and reinforcement-learning contracts |
| Trading safeguards | Testnet-first operation, dual mainnet gates, quantity checks, and idempotent order logs |

The public application contains five areas: Overview, Markets, Orders, Strategies, and Settings. Internal evaluation remains available for research, but there is no public backtest page or `/api/backtest/*` API.

While Settings is open, outbound-IP detection runs immediately and then once per minute, retaining the previous result until the next check completes. The global exchange selection applies consistently to business pages such as Overview, Markets, and Orders; the header provides global symbol selection and a visible Refresh action for the current page.

## Architecture

| Layer | Stack | Location |
| --- | --- | --- |
| Backend API | Python 3.12, FastAPI, Pandas | `backend/app/` |
| Strategies | Automated runtimes, offline Backtrader evaluation | `backend/app/strategies/` |
| Frontend | React 18, Vite, Tailwind CSS | `frontend/src/` |
| Deployment | Docker Compose, Nginx | `docker-compose.yml` |
| External data | Candles, runtime state, and research reports | `G:/CandleMind/CandleMind_data` |

Production market data, databases, secrets, and generated reports do not belong in Git. See [`docs/DATA_LAYOUT.md`](docs/DATA_LAYOUT.md) for the data contract.

## Quick Start

### Docker Compose

Install Docker Desktop, then run:

```powershell
Copy-Item .env.example .env
powershell -ExecutionPolicy Bypass -File ops/dev-compose.ps1
```

Open the web app at <http://localhost:3000>, the API at <http://localhost:8000>, or check <http://localhost:8000/api/ping>.

Override the external data paths in `.env` when necessary:

```dotenv
CANDLEMIND_DATA_ROOT=D:/CandleMind/data
CANDLEMIND_RUNTIME_ROOT=D:/CandleMind/runtime/app
```

### Local Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt
python -m uvicorn backend.app.main:app --reload --env-file .env --port 8000
```

Start the frontend in another terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Vite runs at <http://localhost:5173> and proxies API requests to the backend.

## Configuration and Safety

1. Create `.env` from `.env.example`; never commit secrets, databases, or runtime logs.
2. Enter Binance and AI provider credentials in Settings. Back up `trader.db` together with `secret.key`.
3. While Settings is open, outbound-IP detection runs once per minute for connection diagnostics; it does not replace exchange API permissions or IP allowlist configuration.
4. Cloud AI Base URLs must use trusted HTTPS hosts. Local providers may use loopback or RFC1918 addresses. See [`docs/AI_CONFIGURATION.md`](docs/AI_CONFIGURATION.md).
5. Mainnet requires completed testnet validation, the server-side switch, and explicit UI confirmation.
6. Independently review the strategy, sizing, leverage, stops, and exchange permissions before risking capital.

See [`docs/BINANCE_RESILIENCE.md`](docs/BINANCE_RESILIENCE.md) for Binance retry, cooldown, IP diagnosis, and order-confirmation rules. See [`docs/EXCHANGE_PROVIDERS.md`](docs/EXCHANGE_PROVIDERS.md) for exchange selection, persistence, and isolation of unavailable providers.

## Reinforcement Learning Research

The repository retains EMA-feature reinforcement-learning infrastructure for trend-following research, including feature engineering, data releases, lifecycle rules, and provenance validation. It is for offline experiments and reproducibility only and is **not connected to online inference, order decisions, or live execution**. See [`docs/research/RL_RESEARCH_STATUS.md`](docs/research/RL_RESEARCH_STATUS.md).

## Testing

```powershell
# Complete isolated verification
powershell -ExecutionPolicy Bypass -File ops/verify.ps1

# Individual checks
python -m pytest backend/tests -q
cd frontend
npm test
npm run build
```

Verification uses temporary data directories and does not modify production data on drive G.

## Repository Layout

```text
CandleMind/
|-- backend/app/        # API, services, strategies, and runtimes
|-- backend/scripts/    # Data maintenance and offline evaluation
|-- backend/tests/      # Unit, contract, security, and regression tests
|-- frontend/src/       # Pages, components, state, and API client
|-- docs/               # Data, research, security, and operations docs
|-- ops/                # Deployment and isolated verification scripts
`-- docker-compose.yml  # Container orchestration
```

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md), [`AGENTS.md`](AGENTS.md), and [`docs/README.md`](docs/README.md) before contributing. Run the complete verification gate before opening a pull request. Report security issues privately as described in [`SECURITY.md`](SECURITY.md).

## Acknowledgement

<table>
  <tr>
    <td align="center" width="240"><a href="https://netapi.cc/"><img src="docs/assets/netapi-logo.png" alt="NetAPI Logo" width="210"></a></td>
    <td>Thanks to <a href="https://netapi.cc/"><strong>NetAPI.cc</strong></a> for providing token support to CandleMind. One API key provides access to mainstream AI models with smart routing and usage-based billing.</td>
  </tr>
</table>

## Community

Join the AI automated trading community to discuss quantitative research, engineering, and risk controls.

<p align="center"><img src="docs/assets/wechat-trading-community.jpg" alt="AI automated trading community QR code" width="360"></p>

[Open the QR code through the CDN if it does not load](https://testingcf.jsdelivr.net/gh/JacobeZhao/CandleMind@main/docs/assets/wechat-trading-community.jpg)

## License

CandleMind is released under the [MIT License](LICENSE). Retain the copyright and license notice when using, modifying, or distributing the project. Third-party dependencies retain their own licenses.
