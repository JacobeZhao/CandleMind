"""
API Key 加密存储 — 使用 Fernet 对称加密
密钥首次启动时自动生成并保存到运行状态目录的 secret.key
"""
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from .runtime_paths import RUNTIME_DATA_DIR

_fernet: Fernet | None = None


def _load_or_create_key(key_file: Path) -> bytes:
    if key_file.exists():
        return key_file.read_bytes()

    generated = Fernet.generate_key()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{key_file.name}.", suffix=".tmp", dir=key_file.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(generated)
            output.flush()
            os.fsync(output.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        try:
            os.link(temporary, key_file)
            return generated
        except FileExistsError:
            return key_file.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet:
        return _fernet

    data_dir = RUNTIME_DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "secret.key"

    key = _load_or_create_key(key_file)

    _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
