import asyncio
from types import SimpleNamespace

from backend.app import main
from backend.app.state import AppState
from backend.app.ws_manager import ConnectionManager


class _DatabaseSession:
    def __init__(self, settings=None):
        self.closed = False
        self.settings = settings

    def query(self, _model):
        return self

    def first(self):
        return self.settings

    def close(self):
        self.closed = True


def test_lifespan_cancels_and_awaits_background_tasks(monkeypatch):
    async def scenario():
        database = _DatabaseSession()
        started = [asyncio.Event(), asyncio.Event()]
        finished = [asyncio.Event(), asyncio.Event()]

        async def run_until_cancelled(index):
            started[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                finished[index].set()

        async def stop():
            return None

        monkeypatch.setattr(main, "init_db", lambda: None)
        monkeypatch.setattr(main, "get_db", lambda: iter((database,)))
        monkeypatch.setattr(
            main.app_state, "broadcast_loop", lambda: run_until_cancelled(0)
        )
        monkeypatch.setattr(main, "_reconnect_loop", lambda: run_until_cancelled(1))
        monkeypatch.setattr(main.binance_ws_client, "stop", stop)
        monkeypatch.setattr(main.strategy_route.bot_engine, "shutdown", stop)

        app = SimpleNamespace(state=SimpleNamespace())
        async with main.lifespan(app):
            await asyncio.gather(*(event.wait() for event in started))
            assert len(app.state.background_tasks) == 2
            assert all(not task.done() for task in app.state.background_tasks)

        assert database.closed
        assert all(task.done() for task in app.state.background_tasks)
        assert all(task.cancelled() for task in app.state.background_tasks)
        assert all(event.is_set() for event in finished)

    asyncio.run(scenario())


def test_broadcast_loop_cancels_and_awaits_polling_tasks(monkeypatch):
    async def scenario():
        state = AppState()
        started = [asyncio.Event(), asyncio.Event()]
        finished = [asyncio.Event(), asyncio.Event()]

        async def run_until_cancelled(index):
            started[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                finished[index].set()

        monkeypatch.setattr(state, "_account_loop", lambda: run_until_cancelled(0))
        monkeypatch.setattr(state, "_orders_loop", lambda: run_until_cancelled(1))

        broadcast_task = asyncio.create_task(state.broadcast_loop())
        await asyncio.gather(*(event.wait() for event in started))
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass

        assert broadcast_task.cancelled()
        assert all(event.is_set() for event in finished)

    asyncio.run(scenario())


def test_broadcast_uses_a_connection_snapshot_and_sends_concurrently():
    async def scenario():
        release = asyncio.Event()

        class BlockingWebSocket:
            def __init__(self):
                self.started = asyncio.Event()
                self.payloads = []

            async def send_json(self, payload):
                self.started.set()
                await release.wait()
                self.payloads.append(payload)

        manager = ConnectionManager(send_timeout=1)
        first = BlockingWebSocket()
        second = BlockingWebSocket()
        late = BlockingWebSocket()
        manager.active.extend((first, second))

        broadcast = asyncio.create_task(manager.broadcast({"type": "status"}))
        await asyncio.wait_for(
            asyncio.gather(first.started.wait(), second.started.wait()), timeout=0.2
        )
        manager.active.append(late)
        release.set()
        await broadcast

        assert first.payloads == [{"type": "status"}]
        assert second.payloads == [{"type": "status"}]
        assert late.payloads == []
        assert manager.active == [first, second, late]

    asyncio.run(scenario())


def test_broadcast_times_out_and_removes_only_failed_connections():
    async def scenario():
        class HealthyWebSocket:
            def __init__(self):
                self.payloads = []

            async def send_json(self, payload):
                self.payloads.append(payload)

        class StalledWebSocket:
            def __init__(self):
                self.cancelled = False

            async def send_json(self, _payload):
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled = True

        manager = ConnectionManager(send_timeout=0.01)
        healthy = HealthyWebSocket()
        stalled = StalledWebSocket()
        manager.active.extend((healthy, stalled))

        await manager.broadcast({"type": "ticker"})

        assert healthy.payloads == [{"type": "ticker"}]
        assert stalled.cancelled
        assert manager.active == [healthy]

    asyncio.run(scenario())


def test_reconnect_loop_skips_binance_for_unavailable_provider(monkeypatch):
    async def scenario():
        settings = SimpleNamespace(
            exchange_provider="okx",
            api_key_test_enc="key",
            api_secret_test_enc="secret",
            api_key_enc=None,
            api_secret_enc=None,
            testnet=True,
        )
        database = _DatabaseSession(settings)
        connect_calls = []

        async def one_iteration(_seconds):
            if database.closed:
                raise asyncio.CancelledError

        async def connect_active(value):
            connect_calls.append(value)

        monkeypatch.setattr(main.asyncio, "sleep", one_iteration)
        monkeypatch.setattr(main, "get_db", lambda: iter((database,)))
        monkeypatch.setattr(
            main.settings,
            "_connect_active",
            connect_active,
            raising=False,
        )
        main.app_state.client = None

        try:
            await main._reconnect_loop()
        except asyncio.CancelledError:
            pass

        assert database.closed
        assert connect_calls == []

    asyncio.run(scenario())


def test_lifespan_keeps_unavailable_provider_disconnected(monkeypatch):
    async def scenario():
        settings = SimpleNamespace(
            exchange_provider="a_share",
            api_key_test_enc="key",
            api_secret_test_enc="secret",
            api_key_main_enc=None,
            api_secret_main_enc=None,
            api_key_enc=None,
            api_secret_enc=None,
            testnet=True,
        )
        database = _DatabaseSession(settings)
        events = []

        async def wait_forever():
            await asyncio.Event().wait()

        async def stop_ws():
            events.append("ws-stopped")

        async def stop_agent():
            events.append("agent-stopped")

        async def restore_agent():
            raise AssertionError("unavailable provider must not restore the market agent")

        async def no_op():
            return None

        monkeypatch.setattr(main, "init_db", lambda: None)
        monkeypatch.setattr(main, "get_db", lambda: iter((database,)))
        monkeypatch.setattr(main.app_state, "client", object())
        monkeypatch.setattr(main.app_state, "exchange_provider", "binance")
        monkeypatch.setattr(main.app_state, "broadcast_loop", wait_forever)
        monkeypatch.setattr(main, "_reconnect_loop", wait_forever)
        monkeypatch.setattr(main.binance_ws_client, "stop", stop_ws)
        monkeypatch.setattr(main.market_agent_manager, "stop", stop_agent)
        monkeypatch.setattr(main.market_agent_manager, "restore", restore_agent)
        monkeypatch.setattr(main.market_agent_manager, "shutdown", no_op)
        monkeypatch.setattr(main.strategy_route.bot_engine, "shutdown", no_op)

        app = SimpleNamespace(state=SimpleNamespace())
        async with main.lifespan(app):
            assert main.app_state.exchange_provider == "a_share"
            assert main.app_state.client is None
            assert events == ["ws-stopped", "agent-stopped"]

        assert database.closed

    asyncio.run(scenario())
