# CandleMind 开发文档

## 一、开发原则

- 前后端严格分离
- 高内聚、低耦合
- UI 不直接请求接口
- 图表组件不感知业务

---

## 二、前端模块职责

| 模块 | 职责 |
|----|----|
| MarketView | 数据调度（切换、加载、实时） |
| KlineChart | 图表渲染 |
| ChartToolbar | 用户交互 |
| marketService | 后端接口适配 |

---

## 三、数据流设计

```
切换 symbol / interval
→ POST /klines/switch
→ GET /klines?limit=10000
→ 图表 setData
→ 轮询 /klines/latest
→ 图表 update
```

---

## 四、指标计算说明

- MA / EMA：主图
- MACD / RSI：副图
- 禁止传 null 给 lightweight-charts

---

## 五、代码规范

- TypeScript 必须有类型
- 禁止跨层引用（组件不能调 service）
- 所有接口集中在 marketService

---

## 六、调试技巧

- 图不显示：先检查 time 是否为 Unix 秒
- 指标报错：检查 value 是否为 number
- 接口失败：先浏览器直连接口