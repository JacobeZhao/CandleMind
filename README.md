# CandleMind

CandleMind is a FastAPI and React application for Binance Futures market
monitoring, SAR+ADX paper execution, and reproducible SAR+ADX backtesting.
Market data, runtime state, reports, and historical artifacts remain under the
external root documented in `docs/DATA_LAYOUT.md`.

## Features

- Five-page UI: dashboard, markets, orders, backtest, and settings.
- K-line chart with PSAR plus ADX/DI defaults and AI market analysis.
- Paper-only SAR+ADX V3 runtime bound to the selected symbol.
- Deterministic and Backtrader SAR pyramid backtests with observed funding.
- Checksum-backed K-line and derivatives synchronization.

## Start

Requirements: Python 3.12, Node.js 20, npm, and either the default
`G:\CandleMind\CandleMind_data` layout or explicit `MARKET_DATA_DIR` and
`DATA_DIR` values.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt
python -m uvicorn backend.app.main:app --reload --env-file .env --port 8000
```

In another terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Vite serves <http://localhost:5173> and proxies the API. For Docker:

```powershell
Copy-Item .env.example .env
powershell -ExecutionPolicy Bypass -File ops/dev-compose.ps1
```

The Compose frontend is available at <http://localhost:3000> and the API at
<http://localhost:8000>.

## Verify

```powershell
powershell -ExecutionPolicy Bypass -File ops/verify.ps1
```

The verifier creates an ignored local data layout and never uses the G drive.
Pass `-InstallFrontend` to force `npm ci`.

## Structure

- `backend/app/routes/`: retained FastAPI endpoints.
- `backend/app/services/`: market data, AI, paper runtime, and backtesting.
- `backend/app/strategies/`: SAR+ADX configuration and execution logic.
- `backend/scripts/data/`: supported data synchronization commands.
- `backend/scripts/evaluation/`: retained SAR+ADX evaluation commands.
- `backend/tests/`: behavior, safety, causality, and parity tests.
- `frontend/src/`: React pages, components, context, and API client.
- `docs/`: current data contracts and SAR+ADX evidence.

Live exchange writes are not enabled. The retained strategy engine is
paper-only.
