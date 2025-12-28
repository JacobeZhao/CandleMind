import os
from pathlib import Path


class EnvLoader:
    def __init__(self, env_file: str = ".env", override: bool = False):
        """
        :param env_file: .env 文件路径
        :param override: 是否覆盖已有环境变量
        """
        self.env_path = Path(env_file)
        self.override = override

    def load(self) -> None:
        if not self.env_path.exists():
            return

        with self.env_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                # 忽略空行和注释
                if not line or line.startswith("#"):
                    continue

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                if key in os.environ and not self.override:
                    continue

                os.environ[key] = value


env_loader = EnvLoader()
