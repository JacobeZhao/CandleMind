# Binance 连接韧性规范

## 适用范围

所有运行时 Binance USD-M 只读请求必须通过
`backend/app/services/binance_usdm_gateway.py`。行情归档下载继续使用各自的数据同步器，
但必须保留校验和验证和既有下载重试。`futures_create_order` 是唯一允许直接调用 SDK
的运行时接口。

## 重试规则

- 普通读取和订单状态确认最多尝试 3 次，总预算 5 秒。
- 连接中断、超时、HTTP `408/429/500/502/503/504` 及明确的临时 Binance
  错误可以重试，并使用指数退避和随机抖动。
- 有效的 `Retry-After` 优先于本地退避；超过总预算时立即返回可重试错误。
- HTTP `418` 表示当前出口 IP 已被限流封禁。所有进程内读取共享冷却时间，
  冷却结束前不得继续探测。优先解析 `-1003` 消息中的 `banned until` 时间；无法取得
  截止时间时使用 120 秒默认值。
- TLS、签名、参数、权限和确定性客户端错误不重试。日志和 API 响应不得包含
  API Key、签名、代理凭据、完整请求 URL 或 Binance 原始响应正文。

## 错误判断

HTTP `451` 或 Binance 明确的地域拒绝信息才可标记为 `binance_geo_restricted`。
HTTP `403` 按 Binance WAF/基础设施策略拒绝处理，不得解释为账户 API 权限不足。
错误 `-2015` 同时可能表示 API Key 无效、USD-M 权限不足或后端出口 IP 未加入
白名单，不能单独据此断言 IP 配置错误。第三方出口 IP/国家查询仅用于诊断，
不得作为交易授权或地域限制判定依据。

## 订单安全

订单创建使用确定性客户端订单 ID。提交结果因超时、断线、`-1006/-1007` 或未知型
HTTP `503` 而不明确时，只能通过订单 ID 查询确认；查询可以有限重试，但不得再次创建
订单。只有 Binance 明确证明请求未被接收（如 `-1008`、限流拒绝或明确失败型 `503`）
时，才允许在同一重试预算内使用完全相同的客户端订单 ID 再次提交。无法确认时必须进入
恢复状态并阻止后续交易，保留执行日志等待人工核对。

策略决策、下单、订单确认和停止平仓共享一个交易临界区。取消 asyncio 包装任务不能视为
工作线程已经停止；启动取消或停止操作必须等待临界区真实完成，之后才能平仓或清理运行时。

## 测试网绑定

USD-M 测试网 REST 固定为 `https://demo-fapi.binance.com`，WebSocket 固定为
`wss://demo-fstream.binance.com`。不得混用旧 testnet REST 与 demo WebSocket。
WebSocket 连接只有持续通过稳定窗口后才可重置重连退避，单个有效行情事件不足以重置。

## 验证与回滚

修改 Binance 调用链后运行：

```powershell
python -m pytest backend/tests -q
cd frontend
npm test
npm run build
cd ..
powershell -ExecutionPolicy Bypass -File ops/verify.ps1
```

上线前先在测试网验证连接中断、`429`、WebSocket 重连和不明确订单响应。若出现重复
订单、持续 `418/429`、错误范围数据覆盖当前页面或敏感信息泄漏，应停止策略并回滚代码；
不得删除或回退执行日志，未知订单必须先在交易所完成核对。
