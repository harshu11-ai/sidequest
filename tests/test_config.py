import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequest.autocorrect.config import (
    ConfigurationError,
    default_config_path,
    load_configuration,
)
from sidequest.autocorrect.corrector import ConservativeCorrector


class ConfigurationTests(unittest.TestCase):
    def test_uses_xdg_config_home(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/example-config"}):
            self.assertEqual(
                default_config_path(),
                Path("/tmp/example-config/sidequest/config.json"),
            )

    def test_missing_default_configuration_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration = load_configuration(Path(directory) / "missing.json")
        self.assertFalse(configuration.exists)
        self.assertEqual(configuration.corrections, {})
        self.assertEqual(configuration.abbreviations, {})

    def test_loads_legacy_default_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": directory}
        ):
            legacy_path = Path(directory) / "cli-autocorrect" / "config.json"
            legacy_path.parent.mkdir()
            legacy_path.write_text('{"corrections":{"teh":"the"}}', encoding="utf-8")

            configuration = load_configuration()

        self.assertEqual(configuration.path, legacy_path)
        self.assertEqual(configuration.corrections, {"teh": "the"})

    def test_loads_personal_corrections_and_abbreviations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "corrections": {"awsome": "awesome"},
                        "abbreviations": {"pr": "pull request (PR)"},
                    }
                ),
                encoding="utf-8",
            )
            configuration = load_configuration(path)

        self.assertTrue(configuration.exists)
        self.assertEqual(configuration.corrections, {"awsome": "awesome"})
        self.assertEqual(configuration.abbreviations, {"pr": "pull request (PR)"})
        corrector = ConservativeCorrector(
            configuration.corrections,
            configuration.abbreviations,
        )
        correction = corrector.suggest("awsome")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "awesome")
        expansion = corrector.suggest("pr")
        self.assertIsNotNone(expansion)
        assert expansion is not None
        self.assertEqual(expansion.replacement, "pull request (PR)")

    def test_reports_invalid_json_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"corrections": {', encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, r"line 1, column"):
                load_configuration(path)

    def test_rejects_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"language": "en"}', encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unknown configuration key"):
                load_configuration(path)

    def test_rejects_unsafe_or_identity_corrections(self) -> None:
        invalid_mappings = (
            {"UseEffect": "useeffect"},
            {"src/file": "file"},
            {"same": "same"},
            {"typo": 42},
        )
        for mapping in invalid_mappings:
            with self.subTest(mapping=mapping), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps({"corrections": mapping}), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_configuration(path)

    def test_rejects_unsafe_abbreviations(self) -> None:
        invalid_mappings = (
            {"PR": "pull request"},
            {"pr": ""},
            {"pr": " pull request"},
            {"pr": "pull request "},
            {"pr": "pr"},
            {"pr": "pull\nrequest"},
            {"pr": "café"},
            {"pr": "x" * 501},
            {"pr": 42},
        )
        for mapping in invalid_mappings:
            with self.subTest(mapping=mapping), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps({"abbreviations": mapping}), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_configuration(path)

    def test_rejects_key_shared_by_correction_and_abbreviation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "corrections": {"pr": "per"},
                        "abbreviations": {"pr": "pull request"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "both a correction and an abbreviation",
            ):
                load_configuration(path)


if __name__ == "__main__":
    unittest.main()
