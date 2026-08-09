"""对象存储封装（MinIO / S3 兼容）。

负责静态资源（图池 logo 等）的上传与公开访问。桶设为公开读，前端通过
nginx 反代的固定 URL（``S3_PUBLIC_BASE_URL`` + key）访问——不走预签名，
URL 永久有效，便于 https 统一入口与缓存。
"""

from __future__ import annotations

import json
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

from .config import Settings

# 桶公开读策略（允许任意人 GET 桶内对象）
_PUBLIC_READ_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Action": ["s3:GetObject"],
            "Resource": ["arn:aws:s3:::{bucket}/*"],
        }
    ],
}


class Storage:
    """MinIO/S3 对象存储封装（桶公开读，固定 URL 访问）。"""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        base = settings.s3_public_base_url.rstrip("/")
        self._public_base = base
        self._client = Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        )

    def ensure_bucket(self) -> None:
        """幂等创建桶并设公开读策略（应用启动时调用）。"""
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
            policy = json.dumps(_PUBLIC_READ_POLICY, separators=(",", ":")).replace(
                "{bucket}", self._bucket
            )
            self._client.set_bucket_policy(self._bucket, policy)
        except S3Error:
            # 测试/未就绪环境宽容处理，不阻断启动
            pass

    def gen_key(self, prefix: str, suffix: str) -> str:
        """生成对象 key，如 ``logos/<uuid>.png``。"""
        name = uuid4().hex
        return f"{prefix}/{name}{suffix}" if suffix else f"{prefix}/{name}"

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """上传字节流，返回 key。"""
        from io import BytesIO

        self._client.put_object(
            self._bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        return key

    def public_url(self, key: str | None) -> str | None:
        """拼固定公开 URL（经 nginx 反代）；key 为 None/空返回 None。"""
        if not key:
            return None
        return f"{self._public_base}/{key}"

    # 向后兼容旧调用名（presigned_url）
    presigned_url = public_url
