import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch, sentinel

from sidequest.autocorrect.config import UserConfiguration
from sidequest.cli import _ROOM_CODE_DISPLAY_SECONDS, _run_doctor, _start_multiplayer, main
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
        )

    @patch("sidequest.chess.lifecycle.prepare_agent_command", return_value=["codex", "prepared"])
    @patch("sidequest.chess.lifecycle.AgentLifecycle")
    @patch("sidequest.chess.companion.ChessCompanion")
    @patch("sidequest.chess.game.ComputerChessGame")
    @patch("sidequest.cli.run_in_pty", return_value=0)
    @patch("sidequest.cli.shutil.which", return_value="/usr/local/bin/codex")
    def test_chess_prepares_lifecycle_and_closes_companion(
        self,
        _which,
        run_in_pty,
        game_type,
        companion_type,
        lifecycle_type,
        prepare_command,
    ) -> None:
        companion = companion_type.return_value
        lifecycle = lifecycle_type.return_value

        result = main(["--chess", "codex"])

        self.assertEqual(result, 0)
        game_type.assert_called_once_with(stockfish_path=None)
        companion_type.assert_called_once_with(game_type.return_value, multiplayer=None)
        lifecycle_type.assert_called_once_with(companion, watch_codex_input=True)
        prepare_command.assert_called_once_with(["codex"], "codex", companion)
        run_in_pty.assert_called_once_with(
            ["codex", "prepared"],
            corrections=True,
            corrector=unittest.mock.ANY,
            on_user_input=lifecycle.user_input,
            on_child_output=lifecycle.child_output,
        )
        companion.close.assert_called_once_with()

    @patch("sidequest.cli.time.sleep")
    @patch("sidequest.chess.multiplayer.RemoteChessGame")
    def test_hosting_pauses_to_show_the_room_code(self, game_type, sleep) -> None:
        game_type.return_value.room_code = "ABC123"

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = _start_multiplayer(None, None, None)

        self.assertIs(result, game_type.return_value)
        self.assertIn("ABC123", stdout.getvalue())
        sleep.assert_called_once_with(_ROOM_CODE_DISPLAY_SECONDS)

    @patch("sidequest.cli.time.sleep")
    @patch("sidequest.chess.multiplayer.RemoteChessGame")
    def test_joining_does_not_pause(self, _game_type, sleep) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            _start_multiplayer("ABC123", None, None)

        self.assertIn("Joined room", stdout.getvalue())
        sleep.assert_not_called()

    @patch("sidequest.cli.time.sleep")
    @patch("sidequest.chess.multiplayer.RemoteChessGame")
    def test_profile_scopes_the_local_seat_path(self, game_type, _sleep) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            _start_multiplayer(None, None, "p1")

        state_path = game_type.call_args.args[1]
        self.assertEqual(state_path.name, "multiplayer-p1.json")

    @patch("sidequest.cli.shutil.which", return_value="/usr/local/bin/codex")
    def test_profile_requires_multiplayer_or_join(self, _which) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["--chess", "--profile", "p1", "codex"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--profile requires --multiplayer or --join", stderr.getvalue())

    @patch("sidequest.cli.shutil.which", return_value="/usr/local/bin/codex")
    def test_rejects_multiplayer_flag_placed_after_application_name(self, _which) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["--chess", "codex", "--multiplayer"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must come before the application name", stderr.getvalue())

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


if __name__ == "__main__":
    unittest.main()
