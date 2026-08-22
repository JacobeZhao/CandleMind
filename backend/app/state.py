"""Global runtime state and low-frequency Binance account polling."""

import asyncio
import time

from binance.client import Client
from loguru import logger

from .services.binance_errors import BinanceGatewayError
from .services.binance_usdm_gateway import BinanceUsdMGateway, gateway_error_detail
from .services.bot_engine import bot_engine
from .services.exchange_provider import (
    BINANCE_PROVIDER,
    ExchangeProvider,
    normalize_exchange_provider,
)
from .ws_manager import manager

ACCOUNT_INTERVAL = 30
ORDERS_INTERVAL = 60
STATUS_INTERVAL = 3


class AppState:
    def __init__(self):
        self.client: Client | None = None
        self.symbol = "BTCUSDT"
        self.exchange_provider: ExchangeProvider = BINANCE_PROVIDER

    def set_client(self, client: Client, symbol: str):
        self.client = client
        self.symbol = symbol
        self.exchange_provider = BINANCE_PROVIDER
        try:
            server_ts = BinanceUsdMGateway(client).server_time()
            client.timestamp_offset = server_ts - int(time.time() * 1000)
        except BinanceGatewayError:
            pass

    def disconnect_exchange(self, provider: str, symbol: str | None = None) -> None:
        self.client = None
        self.exchange_provider = normalize_exchange_provider(provider)
        if symbol is not None:
            self.symbol = symbol

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
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(self._account_loop(), name="account-broadcast-loop")
            task_group.create_task(self._orders_loop(), name="orders-broadcast-loop")
            while True:
                await asyncio.sleep(STATUS_INTERVAL)
                if manager.active:
                    await self._push_bot_status()

    async def _account_loop(self):
        while True:
            await asyncio.sleep(ACCOUNT_INTERVAL)
            if (
                self.exchange_provider == BINANCE_PROVIDER
                and self.client
                and manager.active
            ):
                await self._push_account()
                await self._push_positions()

    async def _orders_loop(self):
        while True:
            await asyncio.sleep(ORDERS_INTERVAL)
            if (
                self.exchange_provider == BINANCE_PROVIDER
                and self.client
                and manager.active
            ):
                await self._push_open_orders()

    async def _push_account(self):
        try:
            account = await asyncio.to_thread(BinanceUsdMGateway(self.client).account)
            await self.publish_account(account)
        except BinanceGatewayError as exc:
            logger.debug("Account push failed: {}", exc.code)
            await manager.broadcast({
                "type": "account_error",
                "data": gateway_error_detail(exc),
            })

    async def _push_positions(self):
        try:
            positions = await asyncio.to_thread(
                BinanceUsdMGateway(self.client).position_information,
                symbol=self.symbol,
            )
            active = [position for position in positions if float(position.get("positionAmt", 0)) != 0]
            await manager.broadcast({"type": "positions", "data": active})
        except BinanceGatewayError as exc:
            logger.debug("Positions push failed: {}", exc.code)
            await manager.broadcast({
                "type": "positions_error",
                "data": gateway_error_detail(exc),
            })
        except (TypeError, ValueError):
            await manager.broadcast({
                "type": "positions_error",
                "data": {
                    "code": "upstream_rejected",
                    "message": "Binance returned an invalid response",
                    "retryable": False,
                },
            })

    async def _push_open_orders(self):
        client = self.client
        symbol = self.symbol
        network = "testnet" if bool(getattr(client, "testnet", False)) else "mainnet"
        gateway = BinanceUsdMGateway(client)
        try:
            open_orders, algo_orders = await asyncio.gather(
                asyncio.to_thread(gateway.open_orders, symbol),
                asyncio.to_thread(gateway.open_algo_orders, symbol),
            )
            orders = [
                {**order, "orderSource": "regular"}
                for order in open_orders
            ]
            orders.extend(self._normalize_algo_order(order) for order in algo_orders)
            await manager.broadcast({
                "type": "open_orders",
                "symbol": symbol,
                "network": network,
                "data": orders,
            })
        except BinanceGatewayError as exc:
            logger.debug("Orders push failed: {}", exc.code)
            await manager.broadcast({
                "type": "open_orders_error",
                "symbol": symbol,
                "network": network,
                "data": gateway_error_detail(exc),
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
