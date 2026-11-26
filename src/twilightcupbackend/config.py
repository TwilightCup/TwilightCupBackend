"""基于环境变量的全局配置，启动时加载一次。

参考 AShareGateway 的 os.getenv 风格，但封装为不可变 dataclass 便于注入与测试。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    """全局配置（运行期不可变）。"""

    mongo_uri: str  # MongoDB 连接串
    db_name: str  # 数据库名
    host: str  # 监听地址
    port: int  # 监听端口
    jwt_secret: str  # JWT 签名密钥
    jwt_algorithm: str  # JWT 算法
    access_token_expire_seconds: int  # 访问令牌有效期（秒）
    log_folder: str  # 日志目录
    log_level: str  # 日志级别
    default_countdown_delay: int  # 默认开始倒计时延迟（秒）
    preload_gate_timeout: int  # 预载门控超时兜底（秒，超时强制开始回合）
    s3_endpoint: str  # MinIO/S3 端点，如 localhost:9000
    s3_access_key: str  # 访问密钥
    s3_secret_key: str  # 秘密密钥
    s3_bucket: str  # 桶名
    s3_secure: bool  # 是否用 https
    s3_public_base_url: (
        str  # 公开访问基址（经 nginx 反代），如 https://bsrserver.org.cn:8443/assets
    )
    locales_dir: str  # 语言文件目录（文件名=语言 id，见 docs/locales.md）
    default_locale: str  # 默认语言 id（比赛未切换时使用）

    @classmethod
    def load(cls) -> Settings:
        dotenv.load_dotenv()
        return cls(
            mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
            db_name=os.getenv("DB_NAME", "twilightcup"),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            jwt_secret=os.getenv("JWT_SECRET", "change-me-in-prod"),
            jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
            access_token_expire_seconds=int(
                os.getenv("ACCESS_TOKEN_EXPIRE_SECONDS", str(60 * 60 * 24))
            ),
            log_folder=os.getenv("LOG_FOLDER", "./logs"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            default_countdown_delay=int(os.getenv("DEFAULT_COUNTDOWN_DELAY", "5")),
            preload_gate_timeout=int(os.getenv("PRELOAD_GATE_TIMEOUT", "60")),
            s3_endpoint=os.getenv("S3_ENDPOINT", "localhost:9000"),
            s3_access_key=os.getenv("S3_ACCESS_KEY", "minioadmin"),
            s3_secret_key=os.getenv("S3_SECRET_KEY", "minioadmin"),
            s3_bucket=os.getenv("S3_BUCKET", "twilightcup"),
            s3_secure=os.getenv("S3_SECURE", "false").lower() == "true",
            s3_public_base_url=os.getenv(
                "S3_PUBLIC_BASE_URL", "http://localhost:9000/twilightcup"
            ),  # 直连含 bucket；nginx 模式不含（转发时加 bucket 前缀）
            locales_dir=os.getenv(
                "LOCALES_DIR", str(Path(__file__).resolve().parents[2] / "locales")
            ),
            default_locale=os.getenv("DEFAULT_LOCALE", "en"),
        )


settings: Settings = Settings.load()
