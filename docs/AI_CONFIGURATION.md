# AI 配置

## 云端 Provider

OpenAI、DeepSeek 等云端 Provider 只能使用应用内置的官方 HTTPS 主机和标准
端口。DeepSeek 默认使用 `https://api.deepseek.com`；历史配置中的
`https://api.deepseek.com/v1` 仍可继续使用。官方主机不依赖 DNS 返回公网地址，
因此兼容 Clash Fake-IP、企业 DNS 和远程代理解析。

系统拒绝非官方云端主机、HTTP、URL 内登录凭据、查询参数和 fragment，并禁止
AI HTTP 客户端自动跟随重定向。

## 本地 Provider

`custom`、`litellm` 和 `ollama` 可连接回环地址、RFC1918 私网及 Docker 的
`host.docker.internal`。链路本地、云元数据、组播、未指定和保留地址仍会被拒绝。
服务默认只绑定本机；不要把允许访问内网的 AI 配置接口直接暴露到不受信任网络。

## 代理与密钥

设置页中的代理同时用于 Binance 和 AI 请求。容器运行时会把代理 URL 中的
`localhost` 或 `127.0.0.1` 改写为 `host.docker.internal`。API Key 使用运行目录
中的 `secret.key` 加密保存，不会通过配置列表接口回显。备份或迁移时应将数据库与
`secret.key` 一起处理，任何日志、截图或聊天中泄露的密钥都必须立即撤销并重发。

## 实时行情助手

行情页右侧可以展开 IDE 式只读行情 Agent；桌面端可拖动分隔条调整宽度，窄屏下
自动改为上下分栏。折叠侧栏、离开页面或切换图表周期都不会停止 Agent。Agent 只
绑定品种，固定在每根 5 分钟 K 线收盘后触发，并对齐分析 `1m/5m/15m/1h/4h/1d`
六个周期的已收盘 K 线。大 K 线和 SAR 转向会合并到同一批次，每批最多调用模型一次。

运行状态和最近 100 条分析保存在
`DATA_DIR/agents/market_agent.json`，默认对应
`G:/CandleMind/CandleMind_data/runtime/app/agents/market_agent.json`。服务重启后会
恢复已启用任务，但不会补跑停机期间的 K 线。v1 状态首次迁移时会备份为
`market_agent.v1.json`；LangGraph 检查点单独保存在
`DATA_DIR/agents/checkpoints/market_analysis.sqlite3`。自动分析和手动提问共享最近
20 条压缩摘要，默认每日合计最多调用模型 300 次，可通过
`CANDLEMIND_MARKET_AGENT_DAILY_LIMIT` 调整。该限制按 UTC 日期重置。

后台 Agent 要求单 Uvicorn worker；第二个 worker 会在启动时明确失败。临时行情或
Provider 故障会指数退避且不补跑历史批次，配置无效或预算耗尽时会保留启用意图并
进入明确的暂停状态。

Agent 不调用订单或策略执行接口。当前应用接口没有用户认证，不得直接暴露到公网；
公网部署前必须由可信反向代理增加认证和访问控制。
