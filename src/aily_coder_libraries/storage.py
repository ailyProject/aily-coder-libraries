from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .index import OUTPUT_FILENAME, Package
from .state import STATE_FILENAME

DEFAULT_INDEX_BUCKET = "ailyblockly"


class StorageConfigError(ValueError):
    """Raised when an object-storage target is not configured safely."""


def _required_environment(name: str, *aliases: str) -> str:
    for candidate in (name, *aliases):
        value = os.environ.get(candidate, "").strip()
        if value:
            return value
    raise StorageConfigError(f"缺少环境变量 {name}")


def _validate_endpoint(value: str, variable_name: str) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise StorageConfigError(f"{variable_name} 必须是有效的 HTTP(S) endpoint")
    return endpoint


@dataclass(frozen=True, slots=True)
class TargetSettings:
    name: str
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    package_bucket: str
    index_bucket: str
    region: str
    stores_state: bool

    def __post_init__(self) -> None:
        if self.index_bucket.casefold() != DEFAULT_INDEX_BUCKET:
            raise StorageConfigError(
                f"{self.name} 的索引 bucket 必须是 {DEFAULT_INDEX_BUCKET}"
            )
        if self.package_bucket.casefold() == self.index_bucket.casefold():
            raise StorageConfigError(
                f"{self.name} 的 package bucket 不能使用 {DEFAULT_INDEX_BUCKET}"
            )


def load_target_settings() -> tuple[TargetSettings, TargetSettings]:
    rustfs = TargetSettings(
        name="rustfs",
        endpoint_url=_validate_endpoint(
            _required_environment("RUSTFS_ENDPOINT"), "RUSTFS_ENDPOINT"
        ),
        access_key_id=_required_environment(
            "RUSTFS_ACCESS_KEY_ID", "RUSTFS_ACCESS_KEY"
        ),
        secret_access_key=_required_environment(
            "RUSTFS_SECRET_ACCESS_KEY", "RUSTFS_SECRET_KEY"
        ),
        package_bucket=_required_environment("RUSTFS_PACKAGE_BUCKET"),
        index_bucket=DEFAULT_INDEX_BUCKET,
        region=(os.environ.get("RUSTFS_REGION", "").strip() or "us-east-1"),
        stores_state=False,
    )

    r2_endpoint = os.environ.get("R2_ENDPOINT", "").strip()
    if not r2_endpoint:
        account_id = _required_environment("R2_ACCOUNT_ID")
        r2_endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    cloudflare = TargetSettings(
        name="cloudflare-r2",
        endpoint_url=_validate_endpoint(r2_endpoint, "R2_ENDPOINT"),
        access_key_id=_required_environment("R2_ACCESS_KEY_ID"),
        secret_access_key=_required_environment("R2_SECRET_ACCESS_KEY"),
        package_bucket=_required_environment("R2_PACKAGE_BUCKET"),
        index_bucket=DEFAULT_INDEX_BUCKET,
        region=(os.environ.get("R2_REGION", "").strip() or "auto"),
        stores_state=True,
    )
    return rustfs, cloudflare


class S3Target:
    """Small S3-compatible adapter shared by RustFS and Cloudflare R2."""

    def __init__(self, settings: TargetSettings, client: Any):
        self.name = settings.name
        self.package_bucket = settings.package_bucket
        self.index_bucket = settings.index_bucket
        self.stores_state = settings.stores_state
        self.client = client
        self.index_key = OUTPUT_FILENAME
        self.state_key = f".state/{STATE_FILENAME}"

    @classmethod
    def create(cls, settings: TargetSettings, *, max_pool_connections: int = 10) -> "S3Target":
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            region_name=settings.region,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                max_pool_connections=max_pool_connections,
                s3={"addressing_style": "path"},
            ),
        )
        return cls(settings, client)

    def package_key(self, package: Package) -> str:
        return package.relative_key

    def package_status(self, package: Package) -> Literal["missing", "match", "conflict"]:
        """Inspect a flat package key without ever treating a conflict as writable."""
        key = self.package_key(package)
        try:
            head = self.client.head_object(Bucket=self.package_bucket, Key=key)
        except Exception as exc:
            if _is_missing_object_error(exc):
                return "missing"
            raise

        if head.get("ContentLength") != package.size:
            return "conflict"
        metadata = head.get("Metadata", {})
        stored_digest = (
            str(metadata.get("sha256", "")).casefold()
            if isinstance(metadata, dict)
            else ""
        )
        if stored_digest == package.sha256.casefold():
            return "match"

        # Existing objects without trustworthy metadata may have been uploaded
        # manually. Hash their body once so identical bytes can be adopted, but
        # never overwrite a different object at the same flat key.
        data = self._read_object_bytes(
            self.package_bucket, key, max_bytes=package.size
        )
        if data is None:
            return "missing"
        return (
            "match"
            if hashlib.sha256(data).hexdigest() == package.sha256.casefold()
            else "conflict"
        )

    def upload_package(self, package: Package, path: Path) -> bool:
        key = self.package_key(package)
        status = self.package_status(package)
        if status == "match":
            return False
        if status == "conflict":
            raise RuntimeError(
                f"{self.name} 已存在不同内容，拒绝覆盖: "
                f"{self.package_bucket}/{key}"
            )
        try:
            with path.open("rb") as package_file:
                self.client.put_object(
                    Bucket=self.package_bucket,
                    Key=key,
                    Body=package_file,
                    ContentType="application/zip",
                    CacheControl="public, max-age=31536000, immutable",
                    Metadata={"sha256": package.sha256},
                    IfNoneMatch="*",
                )
        except Exception as exc:
            if not _is_precondition_failed_error(exc):
                raise
            # Another writer won the race after package_status(). Adopt only
            # identical bytes; a conflicting flat key remains immutable.
            if self.package_status(package) == "match":
                return False
            raise RuntimeError(
                f"{self.name} 并发创建了不同内容，拒绝覆盖: "
                f"{self.package_bucket}/{key}"
            ) from exc
        self._assert_object(
            self.package_bucket, key, package.size, package.sha256
        )
        return True

    def document_matches(self, key: str, size: int, sha256: str) -> bool:
        return self._object_matches(self._document_bucket(key), key, size, sha256)

    def _document_bucket(self, key: str) -> str:
        if key == self.state_key:
            if not self.stores_state:
                raise ValueError(f"{self.name} 不负责存储 state")
            return self.package_bucket
        if key == self.index_key:
            return self.index_bucket
        raise ValueError(f"不支持的文档 key: {key}")

    def _object_matches(
        self, bucket: str, key: str, size: int, sha256: str
    ) -> bool:
        try:
            head = self.client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            if _is_missing_object_error(exc):
                return False
            raise
        metadata = head.get("Metadata", {})
        return (
            head.get("ContentLength") == size
            and isinstance(metadata, dict)
            and str(metadata.get("sha256", "")).casefold() == sha256.casefold()
        )

    def read_document_bytes(self, key: str, *, max_bytes: int) -> bytes | None:
        return self._read_object_bytes(
            self._document_bucket(key), key, max_bytes=max_bytes
        )

    def _read_object_bytes(
        self, bucket: str, key: str, *, max_bytes: int
    ) -> bytes | None:
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须大于 0")
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
        except Exception as exc:
            if _is_missing_object_error(exc):
                return None
            raise
        announced_size = response.get("ContentLength")
        if isinstance(announced_size, int) and announced_size > max_bytes:
            raise RuntimeError(f"{self.name} 对象超过读取上限: {bucket}/{key}")
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise RuntimeError(f"{self.name} 返回了无效对象体: {bucket}/{key}")
        try:
            data = body.read(max_bytes + 1)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if not isinstance(data, bytes):
            raise RuntimeError(f"{self.name} 返回了非二进制对象体: {bucket}/{key}")
        if len(data) > max_bytes:
            raise RuntimeError(f"{self.name} 对象超过读取上限: {bucket}/{key}")
        return data

    def upload_document_bytes(
        self,
        key: str,
        data: bytes,
        *,
        sha256: str,
        content_type: str,
        cache_control: str,
    ) -> None:
        bucket = self._document_bucket(key)
        self.client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl=cache_control,
            Metadata={"sha256": sha256},
        )
        self._assert_object(bucket, key, len(data), sha256)

    def _assert_object(
        self, bucket: str, key: str, size: int, sha256: str
    ) -> None:
        if not self._object_matches(bucket, key, size, sha256):
            raise RuntimeError(f"{self.name} 上传后校验失败: {bucket}/{key}")


def _is_missing_object_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(error.get("Code", "")) if isinstance(error, dict) else ""
    return status == 404 or code in {"404", "NoSuchKey", "NotFound"}


def _is_precondition_failed_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(error.get("Code", "")) if isinstance(error, dict) else ""
    return status == 412 or code in {"412", "PreconditionFailed"}
