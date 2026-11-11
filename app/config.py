from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部配置来自环境变量或项目根目录下的 .env。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Emby：默认留空，避免「看起来已经配好了」的假象；
    # 正常使用请在网页设置里填写，只有特殊场景才用环境变量兜底。
    emby_base_url: str = ""
    emby_api_key: str = ""

    # HTTP 服务
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    # 存储与扫描
    db_path: str = "./data/app.db"
    page_size: int = 500
    request_timeout: float = 30.0

    # 定时任务
    scheduler_enabled: bool = True
    scan_cron_incremental: str = "0 3 * * *"
    scan_cron_full: str = "0 3 * * 0"

    @property
    def base_url(self) -> str:
        return self.emby_base_url.rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.emby_api_key)


settings = Settings()
