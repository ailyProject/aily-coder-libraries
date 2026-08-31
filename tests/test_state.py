from __future__ import annotations

import copy
import hashlib
import json
import unittest

from aily_coder_libraries.state import (
    LoadedState,
    StateError,
    new_state,
    parse_state,
    serialise_state,
)


REGISTRY_DIGEST = hashlib.sha256(b"ordered repository list\n").hexdigest()
REPOSITORY_KEY = "github.com/aily/example"
TAG_REF_OID = "a" * 40
TAG_COMMIT_OID = "b" * 40
ARCHIVE_FILE_NAME = "Example-1.0.0.zip"


def release_entry() -> dict[str, object]:
    payload = b"example zip"
    return {
        "name": "Example",
        "version": "1.0.0",
        "author": "Aily",
        "maintainer": "Aily <info@example.com>",
        "sentence": "An example library.",
        "category": "Other",
        "architectures": ["*"],
        "types": ["Arduino"],
        "repository": "https://github.com/aily/example.git",
        "url": f"https://cdn.example/{ARCHIVE_FILE_NAME}",
        "archiveFileName": ARCHIVE_FILE_NAME,
        "size": len(payload),
        "checksum": f"SHA-256:{hashlib.sha256(payload).hexdigest()}",
    }


def released_state() -> LoadedState:
    empty = new_state(REGISTRY_DIGEST)
    document = copy.deepcopy(empty.document)
    document.update(
        {
            "generation": 1,
            "parentDigest": empty.digest,
            "cursor": 1,
            "repositories": {
                REPOSITORY_KEY: {
                    "url": "https://github.com/aily/example.git",
                    "name": "Example",
                    "tags": {
                        "1.0.0": {
                            "refOid": TAG_REF_OID,
                            "commitOid": TAG_COMMIT_OID,
                            "archiveFileName": ARCHIVE_FILE_NAME,
                        }
                    },
                }
            },
            "releases": [
                {
                    "repositoryKey": REPOSITORY_KEY,
                    "tag": "1.0.0",
                    "tagRefOid": TAG_REF_OID,
                    "tagCommitOid": TAG_COMMIT_OID,
                    "entry": release_entry(),
                }
            ],
        }
    )
    return parse_state(serialise_state(document))


class StateDocumentTests(unittest.TestCase):
    def test_new_state_and_canonical_round_trip(self) -> None:
        created = new_state(REGISTRY_DIGEST)

        self.assertEqual(created.document["schemaVersion"], 1)
        self.assertEqual(created.document["generation"], 0)
        self.assertIsNone(created.document["parentDigest"])
        self.assertEqual(created.document["registryDigest"], REGISTRY_DIGEST)
        self.assertEqual(created.document["retryRepositories"], {})
        self.assertEqual(created.document["repositories"], {})
        self.assertEqual(created.document["releases"], [])

        loaded = parse_state(created.data)
        self.assertEqual(loaded.document, created.document)
        self.assertEqual(loaded.data, created.data)
        self.assertEqual(loaded.digest, created.digest)

    def test_corruption_fails_closed(self) -> None:
        valid = released_state()
        unknown_field = copy.deepcopy(valid.document)
        unknown_field["unexpected"] = True
        invalid_checksum = copy.deepcopy(valid.document)
        invalid_checksum["releases"][0]["entry"]["checksum"] = "SHA-256:invalid"

        corrupt_payloads = (
            b"not json",
            b'{"schemaVersion":1,"schemaVersion":1}',
            json.dumps(unknown_field).encode(),
            json.dumps(invalid_checksum).encode(),
        )
        for payload in corrupt_payloads:
            with self.subTest(payload=payload[:40]), self.assertRaises(StateError):
                parse_state(payload)

    def test_non_commit_tag_can_be_remembered_without_a_release(self) -> None:
        state = released_state()
        document = copy.deepcopy(state.document)
        document["repositories"][REPOSITORY_KEY]["tags"]["tree-object"] = {
            "refOid": "c" * 40,
            "commitOid": None,
            "archiveFileName": None,
        }

        loaded = parse_state(serialise_state(document))

        tag = loaded.document["repositories"][REPOSITORY_KEY]["tags"][
            "tree-object"
        ]
        self.assertIsNone(tag["commitOid"])
        self.assertIsNone(tag["archiveFileName"])
        self.assertEqual(len(loaded.document["releases"]), 1)

    def test_release_must_match_its_repository_tag(self) -> None:
        valid = released_state()
        mutations = {
            "missing repository": lambda document: document["releases"][0].update(
                repositoryKey="github.com/aily/missing"
            ),
            "missing tag": lambda document: document["releases"][0].update(
                tag="2.0.0"
            ),
            "ref oid mismatch": lambda document: document["releases"][0].update(
                tagRefOid="c" * 40
            ),
            "commit oid mismatch": lambda document: document["releases"][0].update(
                tagCommitOid="c" * 40
            ),
            "archive mismatch": lambda document: document["releases"][0][
                "entry"
            ].update(archiveFileName="Example-1.0.0-other.zip"),
        }

        for description, mutate in mutations.items():
            document = copy.deepcopy(valid.document)
            mutate(document)
            with self.subTest(description=description), self.assertRaises(StateError):
                serialise_state(document)

    def test_retry_repositories_are_strictly_validated(self) -> None:
        valid = released_state()
        mutations = {
            "missing repository": {"github.com/aily/missing": 1},
            "zero attempts": {REPOSITORY_KEY: 0},
            "boolean attempts": {REPOSITORY_KEY: True},
        }

        for description, retries in mutations.items():
            document = copy.deepcopy(valid.document)
            document["retryRepositories"] = retries
            with self.subTest(description=description), self.assertRaises(StateError):
                serialise_state(document)

    def test_release_version_and_archive_name_must_be_canonical(self) -> None:
        valid = released_state()

        noncanonical_version = copy.deepcopy(valid.document)
        noncanonical_version["releases"][0]["entry"]["version"] = "1.0"
        with self.assertRaisesRegex(StateError, "version 必须使用规范化版本"):
            serialise_state(noncanonical_version)

        wrong_archive = copy.deepcopy(valid.document)
        wrong_name = "Different-1.0.0.zip"
        wrong_archive["releases"][0]["entry"]["archiveFileName"] = wrong_name
        wrong_archive["repositories"][REPOSITORY_KEY]["tags"]["1.0.0"][
            "archiveFileName"
        ] = wrong_name
        with self.assertRaisesRegex(StateError, "archiveFileName 必须是"):
            serialise_state(wrong_archive)

if __name__ == "__main__":
    unittest.main()
