import time
import requests
from pprint import pprint

BASE_URL = "http://127.0.0.1:8000/auth"

access_token: str | None = None


# ================== 工具函数 ==================

def title(text):
    print("\n" + "=" * 60)
    print(f"🔐 {text}")
    print("=" * 60)


def safe_get(path, params=None, headers=None):
    try:
        r = requests.get(
            f"{BASE_URL}{path}",
            params=params,
            headers=headers,
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("❌ 请求失败：", e)
        return None


def safe_post(path, json=None, headers=None):
    try:
        r = requests.post(
            f"{BASE_URL}{path}",
            json=json,
            headers=headers,
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("❌ 请求失败：", e)
        return None


def show_login_state():
    print("\n👤 当前登录状态：")
    if access_token:
        print("   ✅ 已登录")
    else:
        print("   ❌ 未登录")


# ================== 功能函数 ==================

def health_check():
    title("健康检查")
    data = safe_get("/health")
    pprint(data)


def register():
    title("用户注册")

    username = input("用户名：").strip()
    email = input("邮箱：").strip()
    password = input("密码：").strip()

    data = safe_post(
        "/register",
        json={
            "username": username,
            "email": email,
            "password": password,
        },
    )

    pprint(data)


def login():
    global access_token

    title("用户登录")

    email = input("邮箱：").strip()
    password = input("密码：").strip()

    data = safe_post(
        "/login",
        json={
            "email": email,
            "password": password,
        },
    )

    if not data:
        return

    access_token = data.get("access_token")
    print("\n🎫 Token：")
    print(access_token)


def profile():
    title("当前用户信息")

    if not access_token:
        print("❌ 请先登录")
        return

    data = safe_get(
        "/profile",
        params={"token": access_token},
    )

    pprint(data)


def logout():
    global access_token
    access_token = None
    print("👋 已退出登录")


# ================== 主循环 ==================

def main():
    print("🎓 欢迎进入【认证系统 CLI 测试工具】")

    while True:
        show_login_state()

        print(
            """
请选择操作：
1️⃣  健康检查
2️⃣  用户注册
3️⃣  用户登录
4️⃣  查看当前用户
5️⃣  退出登录
0️⃣  退出程序
"""
        )

        choice = input("请输入你的选择：").strip()

        if choice == "1":
            health_check()

        elif choice == "2":
            register()

        elif choice == "3":
            login()

        elif choice == "4":
            profile()

        elif choice == "5":
            logout()

        elif choice == "0":
            print("👋 再见")
            break

        else:
            print("❌ 无效选择，请重新输入")

        time.sleep(0.5)


if __name__ == "__main__":
    main()
