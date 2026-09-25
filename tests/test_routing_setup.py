import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequest.autocorrect.config import (
    ConfigurationError,
    load_configuration,
    set_routing_enabled,
)
from sidequest.routing.credentials import (
    CredentialsError,
    key_file_path,
    resolve_api_key,
    save_api_key,
)
from sidequest.routing.wizard import run_setup


class TemporaryConfig(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "sidequest" / "config.json"
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TYPESAFE_API_KEY", None)


class CredentialsTests(TemporaryConfig):
    def test_no_key_anywhere(self) -> None:
        self.assertIsNone(resolve_api_key(self.config))

    def test_saved_key_is_private_and_can_be_read_back(self) -> None:
        path = save_api_key(self.config, "  secret-key \n")
        self.assertEqual(path, key_file_path(self.config))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        key = resolve_api_key(self.config)
        self.assertEqual((key.value, key.from_environment), ("secret-key", False))

    def test_the_environment_wins_over_the_file(self) -> None:
        save_api_key(self.config, "from-file")
        os.environ["TYPESAFE_API_KEY"] = "from-env"
        key = resolve_api_key(self.config)
        self.assertEqual((key.value, key.from_environment), ("from-env", True))

    def test_a_key_file_other_users_can_read_is_refused(self) -> None:
        path = save_api_key(self.config, "secret-key")
        path.chmod(0o644)
        with self.assertRaisesRegex(CredentialsError, "chmod 600"):
            resolve_api_key(self.config)

    def test_saving_replaces_an_existing_key(self) -> None:
        save_api_key(self.config, "old")
        save_api_key(self.config, "new")
        self.assertEqual(resolve_api_key(self.config).value, "new")
        self.assertEqual([p.name for p in self.config.parent.iterdir()], ["typesafe_key"])

    def test_an_empty_key_file_counts_as_no_key(self) -> None:
        save_api_key(self.config, "")
        self.assertIsNone(resolve_api_key(self.config))


class RoutingConfigurationTests(TemporaryConfig):
    def test_enabled_defaults_to_off(self) -> None:
        self.assertFalse(load_configuration(self.config).routing.enabled)

    def test_setting_it_keeps_everything_else_in_the_file(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            json.dumps({"corrections": {"teh": "the"}, "routing": {"min_confidence": 0.8}}),
            encoding="utf-8",
        )
        set_routing_enabled(self.config, True)
        loaded = load_configuration(self.config)
        self.assertTrue(loaded.routing.enabled)
        self.assertEqual(loaded.routing.min_confidence, 0.8)
        self.assertEqual(loaded.corrections, {"teh": "the"})

    def test_creates_the_file_and_folder_when_missing(self) -> None:
        set_routing_enabled(self.config, True)
        self.assertTrue(load_configuration(self.config).routing.enabled)

    def test_does_not_overwrite_a_file_it_cannot_parse(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            set_routing_enabled(self.config, True)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{not json")

    def test_enabled_must_be_a_boolean(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{"routing": {"enabled": "yes"}}', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_configuration(self.config)


class FakeClassifier:
    def __init__(self, works: bool) -> None:
        self.works = works

    def classify(self, prompt: str):
        return object() if self.works else None


class SetupTests(TemporaryConfig):
    def run_setup(self, answers: list[str], secret: str = "new-key", works: bool = True):
        said: list[str] = []
        replies = iter(answers)
        code = run_setup(
            str(self.config),
            ask=lambda prompt: next(replies),
            ask_secret=lambda prompt: secret,
            say=said.append,
            make_classifier=lambda key: FakeClassifier(works),
        )
        return code, "\n".join(said)

    def enabled(self) -> bool:
        return load_configuration(self.config).routing.enabled

    def test_no_turns_routing_off_and_asks_for_no_key(self) -> None:
        code, _ = self.run_setup(["n"])
        self.assertEqual(code, 0)
        self.assertFalse(self.enabled())
        self.assertIsNone(resolve_api_key(self.config))

    def test_the_default_answer_is_no(self) -> None:
        code, _ = self.run_setup([""])
        self.assertEqual(code, 0)
        self.assertFalse(self.enabled())

    def test_yes_saves_a_verified_key_and_turns_routing_on(self) -> None:
        code, output = self.run_setup(["y"], secret="  new-key  ")
        self.assertEqual(code, 0)
        self.assertTrue(self.enabled())
        self.assertEqual(resolve_api_key(self.config).value, "new-key")
        self.assertIn("The key works", output)
        self.assertNotIn("new-key", output)  # never echoed back

    def test_an_unaccepted_key_is_not_saved_unless_asked(self) -> None:
        code, _ = self.run_setup(["y", "n"], works=False)
        self.assertEqual(code, 1)
        self.assertFalse(self.enabled())
        self.assertIsNone(resolve_api_key(self.config))

    def test_an_unaccepted_key_can_be_saved_anyway(self) -> None:
        code, _ = self.run_setup(["y", "y"], works=False)
        self.assertEqual(code, 0)
        self.assertTrue(self.enabled())

    def test_an_empty_key_changes_nothing(self) -> None:
        code, _ = self.run_setup(["y"], secret="   ")
        self.assertEqual(code, 1)
        self.assertFalse(self.enabled())

    def test_an_existing_key_can_be_kept(self) -> None:
        save_api_key(self.config, "kept-key")
        code, output = self.run_setup(["y", "y"], secret="unused")
        self.assertEqual(code, 0)
        self.assertEqual(resolve_api_key(self.config).value, "kept-key")
        self.assertIn("Found a TypeSafe API key", output)

    def test_an_existing_key_can_be_replaced(self) -> None:
        save_api_key(self.config, "old-key")
        code, _ = self.run_setup(["y", "n"], secret="new-key")
        self.assertEqual(code, 0)
        self.assertEqual(resolve_api_key(self.config).value, "new-key")

    def test_a_key_from_the_environment_is_used_and_not_copied_to_disk(self) -> None:
        os.environ["TYPESAFE_API_KEY"] = "env-key"
        code, output = self.run_setup(["y", "y"])
        self.assertEqual(code, 0)
        self.assertFalse(key_file_path(self.config).exists())
        self.assertIn("keep that set", output)

    def test_answering_no_after_being_on_turns_it_off_and_keeps_the_key(self) -> None:
        self.run_setup(["y"])
        code, _ = self.run_setup(["n"])
        self.assertEqual(code, 0)
        self.assertFalse(self.enabled())
        self.assertEqual(resolve_api_key(self.config).value, "new-key")

    def test_asks_again_after_an_unclear_answer(self) -> None:
        code, _ = self.run_setup(["maybe", "n"])
        self.assertEqual(code, 0)

    def test_a_broken_config_file_is_reported_not_overwritten(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("{not json", encoding="utf-8")
        code, output = self.run_setup(["y"])
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{not json")
        self.assertIn("invalid JSON", output)


if __name__ == "__main__":
    unittest.main()
