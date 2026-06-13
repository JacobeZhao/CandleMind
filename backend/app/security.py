"""
API Key 加密存储 — 使用 Fernet 对称加密
密钥首次启动时自动生成并保存到 data/secret.key
"""
import os
from pathlib import Path
from cryptography.fernet import Fernet

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet:
        return _fernet

    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "secret.key"

    if key_file.exists():
        key = key_file.read_bytes()
    else:
        key = Fernet.generate_key()
        key_file.write_bytes(key)

    _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
