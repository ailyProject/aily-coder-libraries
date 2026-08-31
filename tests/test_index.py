from __future__ import annotations

import hashlib
import unittest

from aily_coder_libraries.index import (
    Dependency,
    IndexBuildError,
    Package,
    archive_stem,
    build_index,
    build_release_entry,
    canonical_repository_url,
    normalise_version,
    parse_library_properties,
    repository_list_digest,
)


def package(archive_file_name: str, payload: bytes = b"zip payload") -> Package:
    return Package(
        archive_file_name=archive_file_name,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def index_entry(
    *,
    name: str,
    version: str,
    archive_file_name: str,
    category: str,
) -> dict[str, object]:
    payload = f"{name}-{version}".encode()
    return {
        "name": name,
        "version": version,
        "author": "Aily",
        "maintainer": "Aily <info@example.com>",
        "sentence": "A test library.",
        "category": category,
        "architectures": ["*"],
        "types": ["Third party"],
        "repository": f"https://github.com/aily/{name}.git",
        "url": f"https://cdn.example/{archive_file_name}",
        "archiveFileName": archive_file_name,
        "size": len(payload),
        "checksum": f"SHA-256:{hashlib.sha256(payload).hexdigest()}",
    }


class LibraryPropertiesTests(unittest.TestCase):
    def test_maps_metadata_lists_and_dependencies(self) -> None:
        metadata = parse_library_properties(
            b"\n".join(
                (
                    b"name=Audio Zero+",
                    b"version=1.2",
                    b"author=Arduino",
                    b"maintainer=Arduino <info@arduino.cc>",
                    b"sentence=Play audio files from an SD card.",
                    b"paragraph=Uses the DAC output.",
                    b"url=https://www.arduino.cc/reference/audio",
                    b"category=Signal Input/Output",
                    b"architectures=samd, avr, samd",
                    b"license=LGPL-2.1-or-later",
                    b"includes=AudioZero.h, AudioZero.hpp, AudioZero.h",
                    b"depends=SD (>=1.0.0, <2.0.0), SPI, Codec (=3.1.0)",
                )
            )
        )

        self.assertEqual(metadata.name, "Audio Zero+")
        self.assertEqual(metadata.version, "1.2.0")
        self.assertEqual(metadata.author, "Arduino")
        self.assertEqual(metadata.maintainer, "Arduino <info@arduino.cc>")
        self.assertEqual(metadata.sentence, "Play audio files from an SD card.")
        self.assertEqual(metadata.paragraph, "Uses the DAC output.")
        self.assertEqual(metadata.website, "https://www.arduino.cc/reference/audio")
        self.assertEqual(metadata.category, "Signal Input/Output")
        self.assertEqual(metadata.architectures, ("samd", "avr"))
        self.assertEqual(metadata.license, "LGPL-2.1-or-later")
        self.assertEqual(
            metadata.provides_includes,
            ("AudioZero.h", "AudioZero.hpp"),
        )
        self.assertEqual(
            metadata.dependencies,
            (
                Dependency("SD", ">=1.0.0, <2.0.0"),
                Dependency("SPI"),
                Dependency("Codec", "=3.1.0"),
            ),
        )

    def test_defaults_architectures_and_normalises_unknown_category(self) -> None:
        metadata = parse_library_properties(
            b"\n".join(
                (
                    b"name=Example",
                    b"version=1.0.0",
                    b"author=Aily",
                    b"maintainer=Aily",
                    b"sentence=Example library.",
                    b"category=Not A Library Manager Category",
                )
            )
        )

        self.assertEqual(metadata.architectures, ("*",))
        self.assertEqual(metadata.category, "Uncategorized")
        self.assertEqual(metadata.dependencies, ())
        self.assertEqual(metadata.provides_includes, ())

    def test_normalises_relaxed_version_and_rejects_invalid_versions(self) -> None:
        self.assertEqual(normalise_version("1.2"), "1.2.0")
        self.assertEqual(normalise_version("7"), "7.0.0")
        self.assertEqual(
            normalise_version("1.2.3-beta.1+build.4"),
            "1.2.3-beta.1+build.4",
        )

        for value in ("", "v1.2.3", "01.2.3", "1.2.3.4", "1.0.0-01"):
            with self.subTest(value=value), self.assertRaises(IndexBuildError):
                normalise_version(value)

    def test_archive_stem_uses_normalised_version_and_safe_name(self) -> None:
        metadata = parse_library_properties(
            b"\n".join(
                (
                    b"name=My Library++/USB",
                    b"version=1.2",
                    b"author=Aily",
                    b"maintainer=Aily",
                    b"sentence=Example library.",
                )
            )
        )

        self.assertEqual(archive_stem(metadata), "My_Library___USB-1.2.0")


class ReleaseEntryTests(unittest.TestCase):
    def test_url_directly_appends_archive_name_and_sets_arduino_type(self) -> None:
        metadata = parse_library_properties(
            b"\n".join(
                (
                    b"name=Audio Zero",
                    b"version=1.2",
                    b"author=Arduino",
                    b"maintainer=Arduino <info@arduino.cc>",
                    b"sentence=Play audio files.",
                    b"url=https://www.arduino.cc/reference/audio",
                    b"architectures=samd",
                )
            )
        )
        archive_file_name = "Audio_Zero-1.2.0.zip"

        entry = build_release_entry(
            metadata,
            "https://github.com/arduino-libraries/AudioZero.git",
            package(archive_file_name),
            "https://cdn.example/new-library-bucket/",
        )

        self.assertEqual(
            entry["url"],
            "https://cdn.example/new-library-bucket/Audio_Zero-1.2.0.zip",
        )
        self.assertEqual(entry["archiveFileName"], archive_file_name)
        self.assertEqual(entry["types"], ["Arduino"])
        self.assertEqual(
            entry["repository"],
            "https://github.com/arduino-libraries/AudioZero.git",
        )


class BuildIndexTests(unittest.TestCase):
    def test_sorting_is_stable_and_latest_version_supplies_category(self) -> None:
        records = [
            {
                "repositoryKey": "github.com/aily/beta",
                "entry": index_entry(
                    name="Beta",
                    version="1.0.0",
                    archive_file_name="Beta-1.0.0.zip",
                    category="Display",
                ),
            },
            {
                "repositoryKey": "github.com/aily/alpha",
                "entry": index_entry(
                    name="Alpha",
                    version="1.9.0",
                    archive_file_name="Alpha-1.9.0.zip",
                    category="Display",
                ),
            },
            {
                "repositoryKey": "github.com/aily/alpha",
                "entry": index_entry(
                    name="Alpha",
                    version="2.0.0-alpha",
                    archive_file_name="Alpha-2.0.0-alpha.zip",
                    category="Data Storage",
                ),
            },
            {
                "repositoryKey": "github.com/aily/alpha",
                "entry": index_entry(
                    name="Alpha",
                    version="2.0.0",
                    archive_file_name="Alpha-2.0.0.zip",
                    category="Sensors",
                ),
            },
        ]

        forward = build_index(records)
        reverse = build_index(reversed(records))

        self.assertEqual(forward, reverse)
        libraries = forward["libraries"]
        self.assertEqual(
            [(item["name"], item["version"]) for item in libraries],
            [
                ("Alpha", "2.0.0"),
                ("Alpha", "2.0.0-alpha"),
                ("Alpha", "1.9.0"),
                ("Beta", "1.0.0"),
            ],
        )
        self.assertEqual(
            [item["category"] for item in libraries if item["name"] == "Alpha"],
            ["Sensors", "Sensors", "Sensors"],
        )
        self.assertTrue(
            all(item["types"] == ["Arduino"] for item in libraries)
        )


class RepositoryIdentityTests(unittest.TestCase):
    def test_canonicalises_equivalent_github_urls(self) -> None:
        self.assertEqual(
            canonical_repository_url("http://GitHub.com/Aily/Example.git/"),
            canonical_repository_url("https://github.com/aily/example"),
        )

    def test_repository_digest_is_ordered_and_uses_canonical_identities(self) -> None:
        first = "https://github.com/Aily/One.git"
        first_equivalent = "http://GITHUB.COM/aily/one/"
        second = "https://gitlab.com/Aily/Two.git"

        self.assertEqual(
            repository_list_digest((first, second)),
            repository_list_digest((first_equivalent, second)),
        )
        self.assertNotEqual(
            repository_list_digest((first, second)),
            repository_list_digest((second, first)),
        )

    def test_repository_digest_rejects_canonical_duplicates(self) -> None:
        with self.assertRaises(IndexBuildError):
            repository_list_digest(
                (
                    "https://github.com/Aily/Example.git",
                    "http://github.com/aily/example/",
                )
            )

    def test_preserves_safe_legacy_repository_fragment(self) -> None:
        self.assertEqual(
            canonical_repository_url(
                "https://github.com/epsilonrt/RadioHead.git#mikem"
            ),
            "github.com/epsilonrt/radiohead#mikem",
        )
        with self.assertRaises(IndexBuildError):
            canonical_repository_url(
                "https://github.com/epsilonrt/RadioHead.git#bad/value"
            )


if __name__ == "__main__":
    unittest.main()
