# CandleMind API 文档

## Base URL

```
http://127.0.0.1:8000
```

---

## 1️⃣ 健康检查

### GET /health

```json
{ "status": "ok" }
```

---

## 2️⃣ 切换行情源

### POST /klines/switch

```json
{
  "symbol": "btcusdt",
  "interval": "1m"
}
```

---

## 3️⃣ 获取历史 K 线

### GET /klines?limit=10000

返回：已收盘 K 线数组

```json
[
  {
    "open_time": "2025-01-01T12:00:00",
    "open": 42000,
    "high": 42100,
    "low": 41950,
    "close": 42050,
    "volume": 12.3
  }
]
```

---

## 4️⃣ 获取最新 K 线

### GET /klines/latest

返回：当前最新一根（可能未收盘）

```json
{
  "open_time": "...",
  "open": 42050,
  "high": 42120,
  "low": 42010,
  "close": 42100,
  "volume": 3.2
}
```