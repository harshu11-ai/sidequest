"""Command-line entry point."""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
from typing import TYPE_CHECKING

from sidequest import __version__
from sidequest.autocorrect.config import ConfigurationError, UserConfiguration, load_configuration
from sidequest.autocorrect.corrector import FrequencyCorrector
from sidequest.pty_proxy import TerminalRequiredError, run_in_pty
from sidequest.routing.credentials import API_KEY_ENV, CredentialsError, resolve_api_key
from sidequest.updater import UpdateError, update_with_pipx

if TYPE_CHECKING:
    from sidequest.routing.session import RoutingSession

SUPPORTED_APPS = {"claude", "codex"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidequest",
        description="Run Claude Code or Codex with prompt autocorrect and wait-time breaks.",
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
        "--breaks",
        action="store_true",
        help="open a small control panel for resumable chess/video breaks while the "
        "agent is working -- chess, video, both, or neither, toggled live from the panel",
    )
    routing_flags = parser.add_mutually_exclusive_group()
    routing_flags.add_argument(
        "--route",
        action="store_true",
        help="use model routing for this run even if 'sidequest setup' has not turned it on: "
        "before each prompt, ask TypeSafe's Jev which model it needs and switch the agent to "
        f"it for this session (sends your prompts to TypeSafe; needs {API_KEY_ENV} or a key "
        "saved by 'sidequest setup')",
    )
    routing_flags.add_argument(
        "--no-route",
        action="store_true",
        help="skip model routing for this run",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check the local installation and exit",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="update, setup, or claude/codex followed by arguments for that application",
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
            or arguments.breaks
            or arguments.route
            or arguments.no_route
        ):
            parser.error("'update' cannot be combined with wrapper options")
        return _run_update()

    if command and command[0] == "setup":
        if len(command) != 1:
            parser.error("'setup' does not accept additional arguments")
        if arguments.no_corrections or arguments.breaks or arguments.route or arguments.no_route:
            parser.error("'setup' can only be combined with --config")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.error("'setup' needs an interactive terminal")
        from sidequest.routing.wizard import run_setup

        return run_setup(arguments.config)

    if not command:
        parser.error("provide either 'claude' or 'codex' to run")

    application = command[0]
    if application not in SUPPORTED_APPS:
        parser.error("sidequest supports only 'claude' and 'codex'")
    if shutil.which(application) is None:
        parser.error(f"could not find '{application}' on PATH")

    configuration = None
    if arguments.route or not arguments.no_corrections:
        configuration = _load_configuration(arguments.config)
        if configuration is None:
            return 2

    # `sidequest setup` turns routing on for every run. --no-corrections is the
    # transparent troubleshooting mode, so it leaves routing off unless --route says otherwise.
    use_routing = arguments.route or (
        configuration is not None
        and configuration.routing.enabled
        and not arguments.no_route
        and not arguments.no_corrections
    )
    routing = None
    if use_routing:
        routing = _build_routing(parser, application, configuration, requested=arguments.route)

    corrector = None
    if not arguments.no_corrections:
        corrector = FrequencyCorrector(
            background=True,
            custom_corrections=configuration.corrections,
            abbreviations=configuration.abbreviations,
        )

    companion = None
    lifecycle = None
    try:
        if arguments.breaks:
            from sidequest.breaks.companion import BreaksCompanion
            from sidequest.lifecycle import AgentLifecycle, prepare_agent_command

            companion = BreaksCompanion()
            companion.open_initial_panel()
            lifecycle = AgentLifecycle(companion, watch_codex_input=application == "codex")
            command = prepare_agent_command(command, application, companion)

        return run_in_pty(
            command,
            corrections=not arguments.no_corrections,
            corrector=corrector,
            on_user_input=lifecycle.user_input if lifecycle else None,
            on_child_output=lifecycle.child_output if lifecycle else None,
            router=routing,
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
        if routing is not None:
            _report_routing(routing)


def _build_routing(
    parser: argparse.ArgumentParser,
    application: str,
    configuration: UserConfiguration,
    *,
    requested: bool,
) -> RoutingSession:
    from sidequest.routing.classifier import JevClassifier, sdk_available
    from sidequest.routing.defaults import DEFAULT_MODELS
    from sidequest.routing.router import DEFAULT_MIN_CONFIDENCE, ModelRouter
    from sidequest.routing.screen import VirtualScreen
    from sidequest.routing.session import RoutingSession

    why = "--route" if requested else "model routing (turned on by 'sidequest setup')"
    skip = "" if requested else "; use --no-route to skip it for this run"
    try:
        key = resolve_api_key(configuration.path)
    except CredentialsError as error:
        parser.error(f"{why} can't use the saved API key: {error}{skip}")
    if key is None:
        parser.error(
            f"{why} needs a TypeSafe API key: set {API_KEY_ENV} or run 'sidequest setup'{skip}"
        )
    if not VirtualScreen.available() or not sdk_available():
        parser.error(
            f"{why} needs pyte and typesafe-sdk: pipx inject sidequest pyte typesafe-sdk{skip}"
        )
    classifier = JevClassifier(key.value)

    settings = configuration.routing
    models = {**DEFAULT_MODELS[application], **settings.models.get(application, {})}
    min_confidence = (
        DEFAULT_MIN_CONFIDENCE if settings.min_confidence is None else settings.min_confidence
    )
    router = ModelRouter(classifier, models, min_confidence=min_confidence)
    return RoutingSession(application, router, VirtualScreen())


def _report_routing(routing: RoutingSession) -> None:
    """Say what routing did, since it works invisibly between prompts."""
    if routing.switches:
        path = " -> ".join(routing.switches)
        count = len(routing.switches)
        print(f"sidequest: routing switched models {count}x ({path})", file=sys.stderr)
    for note in routing.notes:
        print(f"sidequest: {note}", file=sys.stderr)


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
    print(f"Routing: {_routing_status(configuration)}")

    if corrector.load_error is not None:
        print(f"Dictionary error: {corrector.load_error}", file=sys.stderr)
        return 1
    return 0


def _routing_status(configuration: UserConfiguration) -> str:
    from sidequest.routing.classifier import sdk_available
    from sidequest.routing.screen import VirtualScreen

    state = "on" if configuration.routing.enabled else "off (run 'sidequest setup' to turn it on)"
    try:
        key = resolve_api_key(configuration.path)
        key_status = f"key from {key.source}" if key else "no API key"
    except CredentialsError as error:
        key_status = str(error)
    packages = {"pyte": VirtualScreen.available(), "typesafe-sdk": sdk_available()}
    missing = [name for name, present in packages.items() if not present]
    details = [key_status] + ([f"missing {', '.join(missing)}"] if missing else [])
    return f"{state}; {'; '.join(details)}"


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
