"""
全局应用状态。
Ticker 由 Binance WebSocket 推送（binance_ws.py），不再 REST 轮询。
账户/仓位/订单保持低频 REST，bot 状态随账户周期一起推送。
"""
import asyncio
from binance.client import Client
from loguru import logger
from .ws_manager import manager
from .services.bot_engine import bot_engine

ACCOUNT_INTERVAL = 30   # 账户/仓位每 30s 刷新
ORDERS_INTERVAL  = 60   # 订单每 60s 刷新
STATUS_INTERVAL  =  3   # bot 状态每 3s 推送


class AppState:
    def __init__(self):
        self.client: Client | None = None
        self.symbol = "BTCUSDT"

    def set_client(self, client: Client, symbol: str):
        import time as _time
        self.client = client
        self.symbol = symbol
        try:
            server_ts = client.futures_time()["serverTime"]
            client.timestamp_offset = server_ts - int(_time.time() * 1000)
        except Exception:
            pass

    @staticmethod
    def account_payload(account: dict) -> dict:
        usdt = next(
            (asset for asset in account.get("assets", []) if asset.get("asset") == "USDT"),
            {},
        )
        return {
            "totalWalletBalance": account.get("totalWalletBalance", "0"),
            "totalUnrealizedProfit": account.get("totalUnrealizedProfit", "0"),
            "totalMarginBalance": account.get("totalMarginBalance", "0"),
            "availableBalance": usdt.get("availableBalance", "0"),
        }

    async def publish_account(self, account: dict) -> dict:
        payload = self.account_payload(account)
        await manager.broadcast({"type": "account", "data": payload})
        return payload

    async def broadcast_loop(self):
        """启动账户/仓位/订单/状态的后台推送任务。Ticker 由 WS 单独处理。"""
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(self._account_loop(), name="account-broadcast-loop")
            task_group.create_task(self._orders_loop(), name="orders-broadcast-loop")
            # bot status 独立高频推送
            while True:
                await asyncio.sleep(STATUS_INTERVAL)
                if manager.active:
                    await self._push_bot_status()

    # ── REST 轮询（低频）────────────────────────────────────────────────────────

    async def _account_loop(self):
        while True:
            await asyncio.sleep(ACCOUNT_INTERVAL)
            if self.client and manager.active:
                await self._push_account()
                await self._push_positions()

    async def _orders_loop(self):
        while True:
            await asyncio.sleep(ORDERS_INTERVAL)
            if self.client and manager.active:
                await self._push_open_orders()

    async def _push_account(self):
        try:
            account = await asyncio.to_thread(self.client.futures_account)
            await self.publish_account(account)
        except Exception as e:
            logger.debug(f"Account push error: {e}")
            await manager.broadcast({
                "type": "account_error",
                "data": {"message": "Binance 账户读取失败，请检查 API Key、合约权限和出口 IP 白名单。"},
            })

    async def _push_positions(self):
        try:
            positions = await asyncio.to_thread(
                self.client.futures_position_information, symbol=self.symbol
            )
            active = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
            await manager.broadcast({"type": "positions", "data": active})
        except Exception as e:
            logger.debug(f"Positions push error: {e}")

    async def _push_open_orders(self):
        client = self.client
        symbol = self.symbol
        network = "testnet" if bool(getattr(client, "testnet", False)) else "mainnet"
        try:
            open_orders, algo_orders = await asyncio.gather(
                asyncio.to_thread(
                    client.futures_get_open_orders, symbol=symbol
                ),
                asyncio.to_thread(
                    client.futures_get_open_algo_orders, symbol=symbol
                ),
            )
            orders = [
                {**order, "orderSource": "regular"}
                for order in list(open_orders or [])
            ]
            orders.extend(
                self._normalize_algo_order(order)
                for order in list(algo_orders or [])
            )
            await manager.broadcast({
                "type": "open_orders",
                "symbol": symbol,
                "network": network,
                "data": orders,
            })
        except Exception as e:
            logger.debug(f"Orders push error: {e}")
            await manager.broadcast({
                "type": "open_orders_error",
                "symbol": symbol,
                "network": network,
                "data": {"message": "Binance open orders are unavailable."},
            })

    @staticmethod
    def _normalize_algo_order(order: dict) -> dict:
        return {
            **order,
            "orderId": order.get("orderId", order.get("algoId")),
            "clientOrderId": order.get("clientOrderId", order.get("clientAlgoId")),
            "type": order.get("type", order.get("orderType")),
            "origQty": order.get("origQty", order.get("quantity", "0")),
            "price": order.get("price", "0"),
            "stopPrice": order.get("stopPrice", order.get("triggerPrice", "0")),
            "status": order.get("status", order.get("algoStatus")),
            "time": order.get("time", order.get("createTime")),
            "orderSource": "algo",
        }

    async def _push_bot_status(self):
        await manager.broadcast({"type": "bot_status", "data": bot_engine.status})


app_state = AppState()
