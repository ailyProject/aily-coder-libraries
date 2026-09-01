from __future__ import annotations

import io
import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from aily_coder_libraries.index import Package
from aily_coder_libraries.state import STATE_FILENAME
from aily_coder_libraries.storage import (
    S3Target,
    StorageConfigError,
    TargetSettings,
    load_target_settings,
)


class StorageConfigurationTests(unittest.TestCase):
    def test_accepts_aliases_and_defaults_index_bucket(self) -> None:
        environment = {
            "RUSTFS_ENDPOINT": "https://s3.example.com",
            "RUSTFS_ACCESS_KEY": "rust-access",
            "RUSTFS_SECRET_KEY": "rust-secret",
            "RUSTFS_PACKAGE_BUCKET": "rust-packages",
            "R2_ACCOUNT_ID": "account-id",
            "R2_ACCESS_KEY_ID": "r2-access",
            "R2_SECRET_ACCESS_KEY": "r2-secret",
            "R2_PACKAGE_BUCKET": "r2-packages",
        }
        with patch.dict(os.environ, environment, clear=True):
            rustfs, cloudflare = load_target_settings()

        self.assertEqual(rustfs.access_key_id, "rust-access")
        self.assertEqual(rustfs.secret_access_key, "rust-secret")
        self.assertEqual(rustfs.package_bucket, "rust-packages")
        self.assertEqual(rustfs.index_bucket, "ailyblockly")
        self.assertEqual(rustfs.region, "us-east-1")
        self.assertFalse(rustfs.stores_state)
        self.assertEqual(
            cloudflare.endpoint_url,
            "https://account-id.r2.cloudflarestorage.com",
        )
        self.assertEqual(cloudflare.package_bucket, "r2-packages")
        self.assertEqual(cloudflare.index_bucket, "ailyblockly")
        self.assertEqual(cloudflare.region, "auto")
        self.assertTrue(cloudflare.stores_state)

    def test_does_not_fall_back_to_index_bucket_for_packages(self) -> None:
        environment = {
            "RUSTFS_ENDPOINT": "https://s3.example.com",
            "RUSTFS_ACCESS_KEY_ID": "rust-access",
            "RUSTFS_SECRET_ACCESS_KEY": "rust-secret",
            "RUSTFS_BUCKET": "ailyblockly",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(
                StorageConfigError, "RUSTFS_PACKAGE_BUCKET"
            ):
                load_target_settings()

        environment.update(
            {
                "RUSTFS_PACKAGE_BUCKET": "rust-packages",
                "R2_ACCOUNT_ID": "account-id",
                "R2_ACCESS_KEY_ID": "r2-access",
                "R2_SECRET_ACCESS_KEY": "r2-secret",
                "R2_BUCKET": "ailyblockly",
            }
        )
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(StorageConfigError, "R2_PACKAGE_BUCKET"):
                load_target_settings()

    def test_rejects_ailyblockly_as_package_bucket(self) -> None:
        with self.assertRaisesRegex(StorageConfigError, "package bucket"):
            TargetSettings(
                name="rustfs",
                endpoint_url="https://s3.example.com",
                access_key_id="access",
                secret_access_key="secret",
                package_bucket="ailyblockly",
                index_bucket="ailyblockly",
                region="us-east-1",
                stores_state=False,
            )

    def test_requires_ailyblockly_as_index_bucket(self) -> None:
        with self.assertRaisesRegex(StorageConfigError, "索引 bucket"):
            TargetSettings(
                name="rustfs",
                endpoint_url="https://s3.example.com",
                access_key_id="access",
                secret_access_key="secret",
                package_bucket="library-packages",
                index_bucket="other-index-bucket",
                region="us-east-1",
                stores_state=False,
            )


class ObjectPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeS3Client()
        self.target = make_target(self.client)

    def test_package_key_uses_fixed_libraries_prefix(self) -> None:
        package = make_package("Example-1.0.0.zip", b"zip")
        self.assertEqual(
            self.target.package_key(package),
            "libraries/Example-1.0.0.zip",
        )

    def test_places_root_index_and_scoped_state(self) -> None:
        self.assertEqual(
            self.target.index_key,
            "libraries-coder-index.json",
        )
        self.assertEqual(
            self.target.state_key,
            f".state/{STATE_FILENAME}",
        )

    def test_routes_package_and_state_index_documents_to_their_buckets(self) -> None:
        payload = b"zip"
        package = make_package("Example-1.0.0.zip", payload)

        with tempfile.TemporaryDirectory() as directory:
            package_path = Path(directory) / package.archive_file_name
            package_path.write_bytes(payload)
            self.assertTrue(self.target.upload_package(package, package_path))

        state_data = b'{"state":true}\n'
        index_data = b'{"libraries":[]}\n'
        self.target.upload_document_bytes(
            self.target.state_key,
            state_data,
            sha256=sha256(state_data).hexdigest(),
            content_type="application/json",
            cache_control="no-store",
        )
        self.target.upload_document_bytes(
            self.target.index_key,
            index_data,
            sha256=sha256(index_data).hexdigest(),
            content_type="application/json",
            cache_control="no-store",
        )

        self.assertTrue(
            self.target.document_matches(
                self.target.state_key,
                len(state_data),
                sha256(state_data).hexdigest(),
            )
        )
        self.assertTrue(
            self.target.document_matches(
                self.target.index_key,
                len(index_data),
                sha256(index_data).hexdigest(),
            )
        )

        self.assertIn(
            ("library-packages", self.target.package_key(package)),
            self.client.objects,
        )
        self.assertIn(
            ("library-packages", self.target.state_key), self.client.objects
        )
        self.assertIn(
            ("ailyblockly", self.target.index_key), self.client.objects
        )
        self.assertNotIn(
            ("ailyblockly", self.target.package_key(package)),
            self.client.objects,
        )
        self.assertNotIn(
            ("ailyblockly", self.target.state_key), self.client.objects
        )

    def test_document_operations_reject_unknown_keys(self) -> None:
        unknown_key = f"prefix/{self.target.index_key}"
        with self.assertRaisesRegex(ValueError, "不支持的文档 key"):
            self.target.document_matches(unknown_key, 0, sha256(b"").hexdigest())
        with self.assertRaisesRegex(ValueError, "不支持的文档 key"):
            self.target.read_document_bytes(unknown_key, max_bytes=100)
        with self.assertRaisesRegex(ValueError, "不支持的文档 key"):
            self.target.upload_document_bytes(
                unknown_key,
                b"{}\n",
                sha256=sha256(b"{}\n").hexdigest(),
                content_type="application/json",
                cache_control="no-store",
            )

        self.assertEqual(self.client.put_requests, [])
        self.assertEqual(self.client.get_requests, [])

    def test_non_owner_rejects_all_state_document_operations(self) -> None:
        target = make_target(self.client, stores_state=False)
        state_data = b"{}\n"
        state_digest = sha256(state_data).hexdigest()

        with self.assertRaisesRegex(ValueError, "不负责存储 state"):
            target.document_matches(target.state_key, len(state_data), state_digest)
        with self.assertRaisesRegex(ValueError, "不负责存储 state"):
            target.read_document_bytes(target.state_key, max_bytes=100)
        with self.assertRaisesRegex(ValueError, "不负责存储 state"):
            target.upload_document_bytes(
                target.state_key,
                state_data,
                sha256=state_digest,
                content_type="application/json",
                cache_control="no-store",
            )

        self.assertEqual(self.client.put_requests, [])
        self.assertEqual(self.client.get_requests, [])

        target.upload_document_bytes(
            target.index_key,
            state_data,
            sha256=state_digest,
            content_type="application/json",
            cache_control="no-store",
        )
        self.assertIn(("ailyblockly", target.index_key), self.client.objects)


class PackageStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeS3Client()
        self.target = make_target(self.client)
        self.payload = b"package bytes"
        self.package = make_package("Example-1.0.0.zip", self.payload)
        self.object_id = (
            "library-packages",
            self.target.package_key(self.package),
        )

    def test_missing(self) -> None:
        self.assertEqual(self.target.package_status(self.package), "missing")

    def test_matches_trusted_sha256_metadata_without_reading_body(self) -> None:
        self.client.objects[self.object_id] = StoredObject(
            self.payload,
            {"sha256": self.package.sha256},
        )

        self.assertEqual(self.target.package_status(self.package), "match")
        self.assertEqual(self.client.get_requests, [])

    def test_adopts_same_content_without_sha256_metadata(self) -> None:
        self.client.objects[self.object_id] = StoredObject(self.payload, {})

        self.assertEqual(self.target.package_status(self.package), "match")
        self.assertEqual(self.client.get_requests, [self.object_id])

    def test_reports_conflict_for_different_content_at_flat_key(self) -> None:
        conflicting_payload = b"different!!!!"
        self.assertEqual(len(conflicting_payload), len(self.payload))
        self.client.objects[self.object_id] = StoredObject(conflicting_payload, {})

        self.assertEqual(self.target.package_status(self.package), "conflict")

    def test_upload_package_never_puts_over_a_conflict(self) -> None:
        conflicting_payload = b"different!!!!"
        self.client.objects[self.object_id] = StoredObject(conflicting_payload, {})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / self.package.archive_file_name
            path.write_bytes(self.payload)
            with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
                self.target.upload_package(self.package, path)

        self.assertEqual(self.client.put_requests, [])
        self.assertEqual(self.client.objects[self.object_id].data, conflicting_payload)

    def test_adopts_identical_object_created_during_conditional_put(self) -> None:
        self.client.race_object = StoredObject(
            self.payload,
            {"sha256": self.package.sha256},
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / self.package.archive_file_name
            path.write_bytes(self.payload)
            self.assertFalse(self.target.upload_package(self.package, path))

        self.assertEqual(self.client.put_requests, [self.object_id])
        self.assertEqual(
            self.client.if_none_match_requests,
            [(self.object_id, "*")],
        )
        self.assertEqual(self.client.objects[self.object_id].data, self.payload)

    def test_rejects_conflicting_object_created_during_conditional_put(self) -> None:
        conflicting_payload = b"different!!!!"
        self.assertEqual(len(conflicting_payload), len(self.payload))
        self.client.race_object = StoredObject(conflicting_payload, {})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / self.package.archive_file_name
            path.write_bytes(self.payload)
            with self.assertRaisesRegex(RuntimeError, "并发创建了不同内容"):
                self.target.upload_package(self.package, path)

        self.assertEqual(self.client.put_requests, [self.object_id])
        self.assertEqual(
            self.client.if_none_match_requests,
            [(self.object_id, "*")],
        )
        self.assertEqual(
            self.client.objects[self.object_id].data,
            conflicting_payload,
        )


class DocumentReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeS3Client()
        self.target = make_target(self.client)

    def test_reads_state_from_package_bucket(self) -> None:
        data = b'{"schemaVersion":1}\n'
        object_id = ("library-packages", self.target.state_key)
        self.client.objects[object_id] = StoredObject(data, {})

        self.assertEqual(
            self.target.read_document_bytes(self.target.state_key, max_bytes=100),
            data,
        )
        self.assertEqual(self.client.get_requests, [object_id])

    def test_reads_index_from_index_bucket_and_returns_none_when_missing(self) -> None:
        data = b'{"libraries":[]}\n'
        object_id = ("ailyblockly", self.target.index_key)
        self.client.objects[object_id] = StoredObject(data, {})

        self.assertEqual(
            self.target.read_document_bytes(self.target.index_key, max_bytes=100),
            data,
        )
        self.assertEqual(self.client.get_requests, [object_id])
        self.assertIsNone(
            self.target.read_document_bytes(self.target.state_key, max_bytes=100)
        )

    def test_rejects_document_larger_than_limit(self) -> None:
        self.client.objects[("ailyblockly", self.target.index_key)] = StoredObject(
            b"too large", {}
        )
        with self.assertRaisesRegex(RuntimeError, "超过读取上限"):
            self.target.read_document_bytes(self.target.index_key, max_bytes=3)


def make_package(name: str, data: bytes) -> Package:
    return Package(
        archive_file_name=name,
        size=len(data),
        sha256=sha256(data).hexdigest(),
    )


def make_target(
    client: "FakeS3Client", *, stores_state: bool = True
) -> S3Target:
    return S3Target(
        TargetSettings(
            name="cloudflare-r2" if stores_state else "rustfs",
            endpoint_url="https://s3.example.com",
            access_key_id="access",
            secret_access_key="secret",
            package_bucket="library-packages",
            index_bucket="ailyblockly",
            region="us-east-1",
            stores_state=stores_state,
        ),
        client=client,
    )


class StoredObject:
    def __init__(self, data: bytes, metadata: dict[str, str]) -> None:
        self.data = data
        self.metadata = metadata


class MissingObjectError(Exception):
    def __init__(self) -> None:
        super().__init__("NoSuchKey")
        self.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }


class PreconditionFailedError(Exception):
    def __init__(self) -> None:
        super().__init__("PreconditionFailed")
        self.response = {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        }


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], StoredObject] = {}
        self.put_requests: list[tuple[str, str]] = []
        self.if_none_match_requests: list[
            tuple[tuple[str, str], object]
        ] = []
        self.get_requests: list[tuple[str, str]] = []
        self.race_object: StoredObject | None = None

    def put_object(self, **request: object) -> None:
        body = request["Body"]
        data = body.read() if hasattr(body, "read") else body
        if not isinstance(data, bytes):
            raise AssertionError("Body must resolve to bytes")
        bucket = request["Bucket"]
        key = request["Key"]
        metadata = request["Metadata"]
        if not isinstance(bucket, str) or not isinstance(key, str):
            raise AssertionError("Bucket and Key must be strings")
        if not isinstance(metadata, dict):
            raise AssertionError("Metadata must be a dictionary")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
            raise AssertionError("Metadata must contain strings")
        object_id = (bucket, key)
        self.put_requests.append(object_id)
        if_none_match = request.get("IfNoneMatch")
        if if_none_match is not None:
            self.if_none_match_requests.append((object_id, if_none_match))
        if if_none_match == "*":
            if self.race_object is not None:
                self.objects[object_id] = self.race_object
                self.race_object = None
            if object_id in self.objects:
                raise PreconditionFailedError()
        self.objects[object_id] = StoredObject(data, metadata)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        try:
            stored = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise MissingObjectError() from exc
        return {"ContentLength": len(stored.data), "Metadata": stored.metadata}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        object_id = (Bucket, Key)
        self.get_requests.append(object_id)
        try:
            stored = self.objects[object_id]
        except KeyError as exc:
            raise MissingObjectError() from exc
        return {
            "ContentLength": len(stored.data),
            "Body": io.BytesIO(stored.data),
        }


if __name__ == "__main__":
    unittest.main()
