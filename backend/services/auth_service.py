import os
from datetime import datetime, timedelta

from passlib.context import CryptContext
from jose import jwt, JWTError
from bson import ObjectId
from pymongo import MongoClient


# =====================================================
# Database（数据库访问层）
# =====================================================

class Database:
    def __init__(self):
        self.client: MongoClient | None = None
        self.db = None
        self.users = None

    def connect(self):
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        db_name = os.getenv("MONGO_DB_NAME", "candlemind")

        self.client = MongoClient(mongo_uri)
        self.db = self.client[db_name]
        self.users = self.db["users"]

        # 初始化索引
        self.users.create_index("email", unique=True)

        print("✅ MongoDB connected")

    def close(self):
        if self.client:
            self.client.close()
            print("🛑 MongoDB disconnected")


# 单例数据库对象
database = Database()


# =====================================================
# AuthService（业务层）
# =====================================================

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    def __init__(self):
        self.ready = False

    async def start(self):
        database.connect()
        self.ready = True
        print("✅ auth service started")

    async def stop(self):
        database.close()
        self.ready = False
        print("🛑 auth service stopped")

    # -------------------------
    # 密码相关
    # -------------------------

    @staticmethod
    def hash_password(password: str) -> str:
        return pwd_context.hash(password)

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        return pwd_context.verify(password, hashed)

    # -------------------------
    # JWT
    # -------------------------

    @staticmethod
    def create_access_token(user_id: str) -> str:
        expire_minutes = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

        expire = datetime.utcnow() + timedelta(minutes=expire_minutes)
        payload = {
            "sub": user_id,
            "exp": expire
        }

        return jwt.encode(
            payload,
            os.getenv("SECRET_KEY", "dev-secret"),
            algorithm=os.getenv("ALGORITHM", "HS256")
        )

    # -------------------------
    # 业务逻辑
    # -------------------------

    @staticmethod
    def register(username: str, email: str, password: str):
        if database.users.find_one({"email": email}):
            raise ValueError("邮箱已注册")

        user = {
            "username": username,
            "email": email,
            "password": AuthService.hash_password(password),
            "created_at": datetime.utcnow(),
            "is_active": True
        }

        database.users.insert_one(user)

    @staticmethod
    def login(email: str, password: str) -> str:
        user = database.users.find_one({"email": email})
        if not user:
            raise ValueError("邮箱或密码错误")

        if not AuthService.verify_password(password, user["password"]):
            raise ValueError("邮箱或密码错误")

        return AuthService.create_access_token(str(user["_id"]))

    @staticmethod
    def verify_token(token: str):
        try:
            payload = jwt.decode(
                token,
                os.getenv("SECRET_KEY", "dev-secret"),
                algorithms=[os.getenv("ALGORITHM", "HS256")]
            )
            user_id = payload.get("sub")
            if not user_id:
                raise ValueError("无效 token")
        except JWTError:
            raise ValueError("token 校验失败")

        user = database.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise ValueError("用户不存在")

        return user
