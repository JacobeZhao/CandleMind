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

```
# CandleMind

CandleMind 是一个智能交易系统，结合了市场数据分析和AI驱动的决策支持。

## 项目结构

- [backend](./backend/) - 后端服务，提供市场数据API和AI分析功能
- [frontend](./frontend/) - 桌面客户端，使用Electron构建，具有桌面监控和AI分析功能
- [docker](./docker/) - Docker配置文件
- [tools](./tools/) - 辅助工具脚本

## 功能特性

### 后端服务
- 实时市场数据获取（基于Binance API）
- 历史K线数据存储和管理
- 多种大语言模型集成（Qwen3、Mistral、Llama3.1等）
- 用户认证服务

### 桌面客户端
- 透明桌面监控功能
- 实时屏幕变化检测
- AI驱动的内容分析
- 用户友好的图形界面

## 快速开始

### 后端服务

1. 安装依赖：
   ```bash
   cd backend
   pip install -r requirements.txt
   ```

2. 启动服务：
   ```bash
   uvicorn app:app --reload --port 8000
   ```

### 桌面客户端

1. 安装依赖：
   ```bash
   cd frontend
   npm install
   ```

2. 启动应用：
   ```bash
   npm start
   ```

## API 文档

详见 [API.md](./API.md)

## 开发指南

详见 [DEVELOPMENT.md](./DEVELOPMENT.md)

## 产品说明

详见 [PRODUCT.md](./PRODUCT.md)
