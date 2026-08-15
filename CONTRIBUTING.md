# 贡献指南

感谢参与 CandleMind。提交改动即表示你同意贡献内容按根目录 MIT License 分发。

## 开发环境

后端使用 Python 3.12，前端使用 Node.js。首次安装：

```powershell
pip install -r backend/requirements-dev.txt
cd frontend
npm ci
```

启动 API 使用 `python -m uvicorn backend.app.main:app --reload --port 8000`，前端在
`frontend/` 下运行 `npm run dev`。

## 修改要求

- 遵循 `AGENTS.md` 的目录、命名、测试和数据边界。
- FastAPI 路由保持轻量；共享逻辑放入 `backend/app/services/` 或对应策略模块。
- React 使用函数组件、PascalCase 组件名和现有 Tailwind 样式。
- 不提交密钥、数据库、下载行情、模型、运行状态或生成报告。
- 生产数据和实验产物放在 `G:/CandleMind/CandleMind_data`，测试仅使用合成 fixture。
- 交易相关修改必须保持 paper-only 默认值，不得引入交易所写操作。

## 验证与提交

提交前运行：

```powershell
python -m pytest backend/tests -q
cd frontend
npm test
npm run build
```

完整门禁为 `powershell -File ops/verify.ps1`。提交信息使用 Conventional Commits，
例如 `feat: add indicator` 或 `fix: reject unsafe order mode`。

Pull Request 应说明改动范围、验证命令、配置或迁移影响，并关联相关 Issue。界面
改动需附桌面和移动端截图；策略或数据契约改动需说明时间因果性、成本口径及回滚
方法。请保持根 README、`docs/` 索引和相关运行文档同步。
