import time
import requests
from pprint import pprint

BASE_URL = "http://127.0.0.1:8000"

current_symbol = "btcusdt"
current_interval = "1d"

symbols_cache = []
intervals_cache = []


# ================== 工具函数 ==================

def title(text):
    print("\n" + "=" * 60)
    print(f"📘 {text}")
    print("=" * 60)


def show_current_state():
    print("\n📊 当前查看视角：")
    print(f"   品种 (symbol)：{current_symbol.upper()}")
    print(f"   周期 (interval)：{current_interval}")


def safe_get(path, params=None):
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("❌ 请求失败：", e)
        return None


# ================== 后端信息 ==================

def load_symbols_and_intervals():
    global symbols_cache, intervals_cache

    symbols_cache = safe_get("/symbols") or []
    intervals_cache = safe_get("/intervals") or []


def show_symbols():
    title("支持的交易对")
    print(", ".join(s.upper() for s in symbols_cache))


def show_intervals():
    title("支持的周期")
    print(", ".join(intervals_cache))


# ================== 功能函数 ==================

def health_check():
    title("健康检查")
    data = safe_get("/health")
    print(data)


def get_latest_kline():
    title("当前 K 线")
    data = safe_get(
        "/klines/latest",
        params={
            "symbol": current_symbol,
            "interval": current_interval,
        },
    )
    pprint(data)


def get_klines():
    title("收盘 K 线")

    limit = input("请输入要查询的 K 线数量（如10）：").strip()
    if not limit.isdigit():
        print("❌ 输入无效，必须是数字")
        return

    data = safe_get(
        "/klines",
        params={
            "symbol": current_symbol,
            "interval": current_interval,
            "limit": int(limit),
        },
    )

    if not data:
        return

    print(f"\n📈 共返回 {len(data)} 根 K 线（展示最后 3 根）")
    for k in data[-3:]:
        print(
            f"{k['open_time']} | "
            f"O:{k['open']} H:{k['high']} "
            f"L:{k['low']} C:{k['close']} V:{k['volume']}"
        )


def change_view():
    global current_symbol, current_interval

    title("切换品种和周期")

    show_symbols()
    symbol = input("请输入交易对：").strip().lower()
    if symbol not in symbols_cache:
        print("❌ 不支持的交易对")
        return

    show_intervals()
    interval = input("请输入周期：").strip()
    if interval not in intervals_cache:
        print("❌ 不支持的周期")
        return

    current_symbol = symbol
    current_interval = interval
    print("✅ 品种或周期已切换")


# ================== 主循环 ==================

def main():
    print("🎓 欢迎进入【行情系统 CLI 测试工具】")

    load_symbols_and_intervals()

    if not symbols_cache or not intervals_cache:
        print("❌ 无法获取后端配置，请确认服务已启动")
        return

    while True:
        show_current_state()

        print(
            """
请选择操作：
1️⃣  健康检查
2️⃣  查看品种
3️⃣  查看周期
4️⃣  查询最新 K 线
5️⃣  查询收盘 K 线
6️⃣  切换品种或周期
0️⃣  退出程序
"""
        )

        choice = input("请输入你的选择：").strip()

        if choice == "1":
            health_check()

        elif choice == "2":
            show_symbols()

        elif choice == "3":
            show_intervals()

        elif choice == "4":
            get_latest_kline()

        elif choice == "5":
            get_klines()

        elif choice == "6":
            change_view()

        elif choice == "0":
            print("👋 再见")
            break

        else:
            print("❌ 无效选择，请重新输入")

        time.sleep(0.5)


if __name__ == "__main__":
    main()
