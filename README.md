# CandleMind 🔥  
> AI 辅助交易系统（实时行情 · 技术指标 · 可解释 AI 决策）

CandleMind 是一个面向加密货币交易的 **AI 辅助分析系统**，  
提供专业级实时 K 线、技术指标（MA / EMA / MACD / RSI）以及可扩展的 AI 决策流程展示。

---

## ✨ 核心特性

- 🚀 实时行情（Binance WebSocket）
- 📈 专业交易级 K 线图（空心阳线 / 实心阴线）
- 📊 主图 + 副图指标体系
- 🔁 切换品种 / 周期自动加载 10000 根历史 K 线
- 🧠 AI 决策流程（规划中）
- 🌐 前后端分离，支持 Docker 一键部署

---

## 🧱 技术栈

### 后端
- Python 3.10+
- FastAPI
- WebSocket（Binance）
- asyncio

### 前端
- Vue 3 + TypeScript
- Vite
- lightweight-charts
- Axios

---

## 📂 项目结构

```
CandleMind/
├── backend/        # FastAPI 后端
├── frontend/       # Vue 前端
├── docker/         # Docker 部署
└── docs/           # 文档
```

---

## 🚀 快速启动（开发模式）

### 后端
```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev
```

访问：[http://localhost:5173](http://localhost:5173)

---

## 🐳 Docker 部署

见 `docker/` 目录。

---

## 📌 Roadmap

* [ ] WebSocket 推送到前端
* [ ] AI 信号（买卖点）
* [ ] 回测 / 历史回放
* [ ] 策略模拟交易

---

## ⚠️ 风险提示

本项目仅用于 **研究与辅助决策**，不构成任何投资建议。