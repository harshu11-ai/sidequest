"""Command-line entry point."""

from __future__ import annotations

import argparse
import platform
import shutil
import sys

from sidequest import __version__
from sidequest.config import ConfigurationError, UserConfiguration, load_configuration
from sidequest.corrector import FrequencyCorrector
from sidequest.pty_proxy import TerminalRequiredError, run_in_pty
from sidequest.updater import UpdateError, update_with_pipx

SUPPORTED_APPS = {"claude", "codex"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidequest",
        description="Run Claude Code or Codex with prompt autocorrect and wait-time games.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--no-corrections",
        action="store_true",
        help="run as a transparent PTY proxy without changing input",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="load personal corrections and abbreviations from PATH",
    )
    parser.add_argument(
        "--chess",
        action="store_true",
        help="open a resumable local chess game while the agent is working",
    )
    parser.add_argument(
        "--stockfish",
        metavar="PATH",
        help="use a specific Stockfish executable for --chess",
    )
    parser.add_argument(
        "--multiplayer",
        action="store_true",
        help="host a new multiplayer chess game over the relay and print a code to share. "
        "Requires --chess.",
    )
    parser.add_argument(
        "--join",
        metavar="CODE",
        help="join a multiplayer chess game using a code you were given. Requires --chess.",
    )
    parser.add_argument(
        "--relay-url",
        metavar="URL",
        help="multiplayer relay to use with --multiplayer/--join (defaults to the built-in relay)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check the local installation and exit",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="update, or claude/codex followed by arguments for that application",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    command = list(arguments.command)
    if command and command[0] == "--":
        command.pop(0)
    if arguments.doctor:
        if command:
            parser.error("--doctor cannot be combined with an application command")
        configuration = _load_configuration(arguments.config)
        if configuration is None:
            return 2
        return _run_doctor(configuration)

    if command and command[0] == "update":
        if len(command) != 1:
            parser.error("'update' does not accept additional arguments")
        if (
            arguments.no_corrections
            or arguments.config is not None
            or arguments.chess
            or arguments.stockfish is not None
            or arguments.multiplayer
            or arguments.join is not None
            or arguments.relay_url is not None
        ):
            parser.error("'update' cannot be combined with wrapper options")
        return _run_update()

    if not command:
        parser.error("provide either 'claude' or 'codex' to run")

    application = command[0]
    if application not in SUPPORTED_APPS:
        parser.error("the prototype currently supports only 'claude' and 'codex'")
    if shutil.which(application) is None:
        parser.error(f"could not find '{application}' on PATH")
    if arguments.stockfish is not None and not arguments.chess:
        parser.error("--stockfish requires --chess")
    if arguments.stockfish is not None and shutil.which(arguments.stockfish) is None:
        parser.error(f"could not find Stockfish executable: {arguments.stockfish}")
    if arguments.multiplayer and arguments.join is not None:
        parser.error("--multiplayer and --join cannot be combined")
    multiplayer_requested = arguments.multiplayer or arguments.join is not None
    if multiplayer_requested and not arguments.chess:
        parser.error("--multiplayer/--join requires --chess")
    if arguments.relay_url is not None and not multiplayer_requested:
        parser.error("--relay-url requires --multiplayer or --join")

    corrector = None
    if not arguments.no_corrections:
        configuration = _load_configuration(arguments.config)
        if configuration is None:
            return 2
        corrector = FrequencyCorrector(
            background=True,
            custom_corrections=configuration.corrections,
            abbreviations=configuration.abbreviations,
        )

    companion = None
    lifecycle = None
    try:
        if arguments.chess:
            from sidequest.chess_companion import ChessCompanion
            from sidequest.chess_game import ComputerChessGame
            from sidequest.lifecycle import AgentLifecycle, prepare_agent_command

            multiplayer_game = None
            if multiplayer_requested:
                multiplayer_game = _start_multiplayer(arguments.join, arguments.relay_url)
                if multiplayer_game is None:
                    return 1

            companion = ChessCompanion(
                ComputerChessGame(stockfish_path=arguments.stockfish),
                multiplayer=multiplayer_game,
            )
            lifecycle = AgentLifecycle(
                companion,
                watch_codex_input=application == "codex",
            )
            command = prepare_agent_command(command, application, companion)

        return run_in_pty(
            command,
            corrections=not arguments.no_corrections,
            corrector=corrector,
            on_user_input=lifecycle.user_input if lifecycle else None,
            on_child_output=lifecycle.child_output if lifecycle else None,
        )
    except TerminalRequiredError as error:
        print(f"sidequest: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"sidequest: terminal I/O failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if companion is not None:
            companion.close()


def _start_multiplayer(code: str | None, relay_url: str | None):
    from sidequest.multiplayer_chess import MultiplayerError, RemoteChessGame
    from sidequest.relay_client import DEFAULT_RELAY_URL, RelayClient, RelayError

    relay = RelayClient(relay_url or DEFAULT_RELAY_URL)
    try:
        game = RemoteChessGame(relay, code=code)
    except (RelayError, MultiplayerError) as error:
        print(f"sidequest: multiplayer setup failed: {error}", file=sys.stderr)
        return None
    if code is None:
        print(f"Share this code with your opponent: {game.room_code}")
    else:
        print(f"Joined room {game.room_code}.")
    return game


def _load_configuration(path: str | None) -> UserConfiguration | None:
    try:
        configuration = load_configuration(path)
        if path is not None and not configuration.exists:
            raise ConfigurationError(f"configuration file does not exist: {configuration.path}")
        return configuration
    except ConfigurationError as error:
        print(f"sidequest: {error}", file=sys.stderr)
        return None


def _run_doctor(configuration: UserConfiguration) -> int:
    corrector = FrequencyCorrector(
        background=False,
        custom_corrections=configuration.corrections,
        abbreviations=configuration.abbreviations,
    )
    dictionary_ready = corrector.wait_until_ready(0)
    config_status = "not created (using built-in defaults)"
    if configuration.exists:
        config_status = (
            f"loaded ({len(configuration.corrections)} personal corrections, "
            f"{len(configuration.abbreviations)} abbreviations)"
        )

    print(f"Sidequest: {__version__}")
    print(f"Python: {platform.python_version()}")
    print(f"Platform: {platform.system()} {platform.machine()}")
    print(f"Config: {configuration.path} — {config_status}")
    print(f"Dictionary: {'ready' if dictionary_ready else 'failed'}")
    for application in sorted(SUPPORTED_APPS):
        executable = shutil.which(application)
        print(f"{application}: {executable or 'not found'}")
    terminal_status = (
        "interactive" if sys.stdin.isatty() and sys.stdout.isatty() else "not interactive"
    )
    print(f"Terminal: {terminal_status}")

    if corrector.load_error is not None:
        print(f"Dictionary error: {corrector.load_error}", file=sys.stderr)
        return 1
    return 0


def _run_update() -> int:
    print("Updating Sidequest from GitHub with pipx...")
    try:
        result = update_with_pipx()
    except UpdateError as error:
        print(f"sidequest: update failed: {error}", file=sys.stderr)
        return 1

    if result.previous_version == result.current_version:
        print(f"Reinstalled Sidequest {result.current_version}.")
    else:
        print(
            f"Updated Sidequest {result.previous_version} -> {result.current_version}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
