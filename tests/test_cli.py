import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, sentinel

from sidequest.autocorrect.config import UserConfiguration
from sidequest.cli import _run_doctor, main
from sidequest.routing.credentials import save_api_key
from sidequest.updater import UpdateError, UpdateResult


class CliTests(unittest.TestCase):
    def test_rejects_unsupported_application(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["bash"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("supports only 'claude' and 'codex'", stderr.getvalue())

    @patch("sidequest.cli.run_in_pty", return_value=7)
    @patch("sidequest.cli.shutil.which", return_value="/usr/local/bin/codex")
    @patch("sidequest.cli.FrequencyCorrector", return_value=sentinel.corrector)
    def test_runs_supported_application(self, frequency_corrector, _which, run_in_pty) -> None:
        result = main(["codex", "--model", "example"])
        self.assertEqual(result, 7)
        frequency_corrector.assert_called_once_with(
            background=True,
            custom_corrections={},
            abbreviations={},
        )
        run_in_pty.assert_called_once_with(
            ["codex", "--model", "example"],
            corrections=True,
            corrector=sentinel.corrector,
            on_user_input=None,
            on_child_output=None,
            router=None,
        )

    @patch("sidequest.cli.run_in_pty", return_value=0)
    @patch("sidequest.cli.shutil.which", return_value="/usr/local/bin/claude")
    def test_can_disable_corrections(self, _which, run_in_pty) -> None:
        result = main(["--no-corrections", "claude"])
        self.assertEqual(result, 0)
        run_in_pty.assert_called_once_with(
            ["claude"],
            corrections=False,
            corrector=None,
            on_user_input=None,
            on_child_output=None,
            router=None,
        )

    @patch("sidequest.lifecycle.prepare_agent_command", return_value=["codex", "prepared"])
    @patch("sidequest.lifecycle.AgentLifecycle")
    @patch("sidequest.breaks.companion.BreaksCompanion")
    @patch("sidequest.cli.run_in_pty", return_value=0)
    @patch("sidequest.cli.shutil.which", return_value="/usr/local/bin/codex")
    def test_breaks_prepares_lifecycle_and_closes_companion(
        self,
        _which,
        run_in_pty,
        companion_type,
        lifecycle_type,
        prepare_command,
    ) -> None:
        companion = companion_type.return_value
        lifecycle = lifecycle_type.return_value

        result = main(["--breaks", "codex"])

        self.assertEqual(result, 0)
        companion_type.assert_called_once_with()
        companion.open_initial_panel.assert_called_once_with()
        lifecycle_type.assert_called_once_with(companion, watch_codex_input=True)
        prepare_command.assert_called_once_with(["codex"], "codex", companion)
        run_in_pty.assert_called_once_with(
            ["codex", "prepared"],
            corrections=True,
            corrector=unittest.mock.ANY,
            on_user_input=lifecycle.user_input,
            on_child_output=lifecycle.child_output,
            router=None,
        )
        companion.close.assert_called_once_with()

    @patch("sidequest.cli.run_in_pty")
    @patch("sidequest.cli.shutil.which", return_value="/usr/local/bin/codex")
    def test_rejects_missing_explicit_config(self, _which, run_in_pty) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(["--config", "/does/not/exist.json", "codex"])
        self.assertEqual(result, 2)
        self.assertIn("configuration file does not exist", stderr.getvalue())
        run_in_pty.assert_not_called()

    @patch("sidequest.cli._run_doctor", return_value=0)
    def test_runs_doctor_without_application(self, run_doctor) -> None:
        self.assertEqual(main(["--doctor"]), 0)
        run_doctor.assert_called_once()

    @patch(
        "sidequest.cli.update_with_pipx",
        return_value=UpdateResult(previous_version="0.2.1", current_version="0.2.2"),
    )
    def test_updates_pipx_installation(self, update_with_pipx) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(["update"])

        self.assertEqual(result, 0)
        self.assertIn("Updated Sidequest 0.2.1 -> 0.2.2.", stdout.getvalue())
        update_with_pipx.assert_called_once_with()

    @patch(
        "sidequest.cli.update_with_pipx",
        return_value=UpdateResult(previous_version="0.2.2", current_version="0.2.2"),
    )
    def test_reports_same_version_reinstall(self, _update_with_pipx) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(["update"])

        self.assertEqual(result, 0)
        self.assertIn("Reinstalled Sidequest 0.2.2.", stdout.getvalue())

    @patch(
        "sidequest.cli.update_with_pipx",
        side_effect=UpdateError("the running copy is not managed by pipx"),
    )
    def test_reports_update_failure(self, _update_with_pipx) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(["update"])

        self.assertEqual(result, 1)
        self.assertIn("update failed", stderr.getvalue())

    def test_rejects_update_arguments(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["update", "extra"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("does not accept additional arguments", stderr.getvalue())

    def test_rejects_update_with_wrapper_options(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["--no-corrections", "update"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("cannot be combined with wrapper options", stderr.getvalue())

    def test_rejects_update_with_breaks(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["--breaks", "update"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("cannot be combined with wrapper options", stderr.getvalue())

    @patch("sidequest.cli.shutil.which", return_value=None)
    @patch("sidequest.cli.FrequencyCorrector")
    def test_doctor_reports_correction_and_abbreviation_counts(
        self,
        frequency_corrector,
        _which,
    ) -> None:
        frequency_corrector.return_value.wait_until_ready.return_value = True
        frequency_corrector.return_value.load_error = None
        configuration = UserConfiguration(
            path=Path("/tmp/config.json"),
            corrections={"teh": "the"},
            abbreviations={"pr": "pull request", "rt": "run tests"},
            exists=True,
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = _run_doctor(configuration)

        self.assertEqual(result, 0)
        self.assertIn("1 personal corrections, 2 abbreviations", stdout.getvalue())
        frequency_corrector.assert_called_once_with(
            background=False,
            custom_corrections={"teh": "the"},
            abbreviations={"pr": "pull request", "rt": "run tests"},
        )


class RoutingCliTests(unittest.TestCase):
    """How --route, --no-route and `sidequest setup` decide whether routing runs."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_home = Path(directory.name)
        self.config = self.config_home / "sidequest" / "config.json"
        environment = patch.dict("os.environ", {"XDG_CONFIG_HOME": directory.name}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for target, kwargs in (
            ("sidequest.cli.shutil.which", {"return_value": "/usr/local/bin/claude"}),
            ("sidequest.cli.FrequencyCorrector", {"return_value": sentinel.corrector}),
            ("sidequest.routing.screen.VirtualScreen", {}),
            ("sidequest.routing.classifier.sdk_available", {"return_value": True}),
            ("sidequest.routing.classifier.JevClassifier", {}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        run = patch("sidequest.cli.run_in_pty", return_value=0)
        self.run_in_pty = run.start()
        self.addCleanup(run.stop)

    def enable_in_config(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{"routing": {"enabled": true}}', encoding="utf-8")

    def routing_passed(self):
        return self.run_in_pty.call_args.kwargs["router"]

    def error_from(self, argv: list[str]) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(argv)
        self.assertEqual(raised.exception.code, 2)
        self.run_in_pty.assert_not_called()
        return stderr.getvalue()

    def test_routing_is_off_by_default(self) -> None:
        main(["claude"])
        self.assertIsNone(self.routing_passed())

    def test_route_flag_without_a_key_is_an_error(self) -> None:
        message = self.error_from(["--route", "claude"])
        self.assertIn("TYPESAFE_API_KEY", message)
        self.assertIn("sidequest setup", message)

    def test_route_flag_with_an_environment_key_turns_routing_on(self) -> None:
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-key"}):
            main(["--route", "claude"])
        self.assertIsNotNone(self.routing_passed())

    def test_setup_turns_it_on_for_every_run_using_the_saved_key(self) -> None:
        self.enable_in_config()
        save_api_key(self.config, "saved-key")
        main(["claude"])
        self.assertIsNotNone(self.routing_passed())

    def test_enabled_without_a_key_says_how_to_fix_it_or_skip_it(self) -> None:
        self.enable_in_config()
        message = self.error_from(["claude"])
        self.assertIn("sidequest setup", message)
        self.assertIn("--no-route", message)

    def test_no_route_skips_it_even_without_a_key(self) -> None:
        self.enable_in_config()
        main(["--no-route", "claude"])
        self.assertIsNone(self.routing_passed())

    def test_no_corrections_leaves_routing_off(self) -> None:
        self.enable_in_config()
        main(["--no-corrections", "claude"])
        self.assertIsNone(self.routing_passed())

    def test_route_and_no_route_conflict(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--route", "--no-route", "claude"])

    def test_a_key_file_other_users_can_read_is_refused(self) -> None:
        self.enable_in_config()
        save_api_key(self.config, "saved-key").chmod(0o644)
        self.assertIn("chmod 600", self.error_from(["claude"]))

    def test_missing_packages_are_reported_with_the_fix(self) -> None:
        self.enable_in_config()
        save_api_key(self.config, "saved-key")
        with patch("sidequest.routing.classifier.sdk_available", return_value=False):
            message = self.error_from(["claude"])
        self.assertIn("pipx inject sidequest pyte typesafe-sdk", message)

    def test_setup_needs_a_terminal(self) -> None:
        self.assertIn("interactive terminal", self.error_from(["setup"]))

    def test_setup_runs_the_wizard(self) -> None:
        with (
            patch("sidequest.cli.sys.stdin.isatty", return_value=True),
            patch("sidequest.cli.sys.stdout.isatty", return_value=True),
            patch("sidequest.routing.wizard.run_setup", return_value=0) as wizard,
        ):
            self.assertEqual(main(["setup"]), 0)
        wizard.assert_called_once_with(None)

    def test_setup_accepts_only_config_alongside_it(self) -> None:
        self.assertIn("only be combined with --config", self.error_from(["--route", "setup"]))
        self.assertIn("does not accept", self.error_from(["setup", "claude"]))


if __name__ == "__main__":
    unittest.main()
