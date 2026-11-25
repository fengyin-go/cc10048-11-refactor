"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "多维指标分析与实验评估平台"
    env: str = "local"
    port: int = 8012
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5176",
            "http://localhost:5176",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 附件落盘目录：监控交付从这里读取实际写出的文件，而不是重新估算。
    attachment_dir: str = "/tmp/s11-delivery"


settings = Settings()
