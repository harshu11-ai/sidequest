"""`sidequest setup`: choose whether to use model routing, and store its API key."""

from __future__ import annotations

import getpass
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from sidequest.autocorrect.config import (
    ConfigurationError,
    load_configuration,
    set_routing_enabled,
)
from sidequest.routing.classifier import JevClassifier, Judgment, sdk_available
from sidequest.routing.credentials import (
    API_KEY_ENV,
    CredentialsError,
    resolve_api_key,
    save_api_key,
)
from sidequest.routing.screen import VirtualScreen

INSTALL_HINT = "pipx inject sidequest pyte typesafe-sdk   (or: pip install 'sidequest[routing]')"
_SAMPLE_PROMPT = "rename the variable foo to bar in utils.py"

_INTRO = """\
Model routing picks a model for each prompt. When you press Enter, TypeSafe's
Jev model rates how demanding the prompt is, and Sidequest switches Claude Code
or Codex to a matching model for that session before the prompt is sent.

It needs a TypeSafe API key, and while it is on, each prompt (the first and
last 4,000 characters of a long one) is sent to TypeSafe to be rated."""


class _Classifier(Protocol):
    def classify(self, prompt: str) -> Judgment | None: ...


def run_setup(
    config_path: str | None = None,
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    say: Callable[[str], None] = print,
    make_classifier: Callable[[str], _Classifier] | None = None,
) -> int:
    """Walk the user through it. Returns a process exit code."""
    try:
        configuration = load_configuration(config_path)
    except ConfigurationError as error:
        say(f"sidequest: {error}")
        return 2

    say(_INTRO)
    say("")
    wants_routing = _confirm(ask, "Use model routing?", default=configuration.routing.enabled)
    try:
        if not wants_routing:
            written = set_routing_enabled(config_path, False)
            say(f"\nModel routing is off ({written}). Run `sidequest setup` again to turn it on.")
            return 0
        return _enable(configuration.path, config_path, ask, ask_secret, say, make_classifier)
    except (ConfigurationError, CredentialsError, OSError) as error:
        say(f"sidequest: {error}")
        return 1


def _enable(
    config_file: Path,
    config_arg: str | None,
    ask: Callable[[str], str],
    ask_secret: Callable[[str], str],
    say: Callable[[str], None],
    make_classifier: Callable[[str], _Classifier] | None,
) -> int:
    reused = None
    key = None
    existing = resolve_api_key(config_file)
    if existing is not None:
        say(f"\nFound a TypeSafe API key ({existing.source}).")
        if _confirm(ask, "Use it?", default=True):
            key, reused = existing.value, existing
    if key is None:
        say("\nPaste your TypeSafe API key (typing is hidden; get one from TypeSafe's console).")
        key = ask_secret("API key: ").strip()
        if not key:
            say("No key entered; nothing changed.")
            return 1

    if not _verified(key, make_classifier, ask, say):
        say("Nothing changed.")
        return 1

    if reused is None:
        saved = save_api_key(config_file, key)
        say(f"Saved the key to {saved} (readable only by you).")
    written = set_routing_enabled(config_arg, True)
    say(f"Turned routing on in {written}.")

    say("\nModel routing is on. From now on `sidequest claude` and `sidequest codex` use it.")
    say("Skip it for one run with `--no-route`; turn it off with `sidequest setup`.")
    if reused is not None and reused.from_environment:
        say(f"It will use ${API_KEY_ENV} from your environment, so keep that set.")
    _report_missing_packages(say)
    return 0


def _verified(
    key: str,
    make_classifier: Callable[[str], _Classifier] | None,
    ask: Callable[[str], str],
    say: Callable[[str], None],
) -> bool:
    """Try the key on a sample prompt. True to go ahead and save it."""
    if make_classifier is None and not sdk_available():
        say("\nCan't check the key yet: the TypeSafe SDK isn't installed.")
        return _confirm(ask, "Save it anyway?", default=True)

    say("\nChecking the key...")
    factory = make_classifier or (lambda value: JevClassifier(value, timeout=5.0))
    if factory(key).classify(_SAMPLE_PROMPT) is not None:
        say("The key works.")
        return True
    say("TypeSafe didn't accept the key, or couldn't be reached.")
    return _confirm(ask, "Save it anyway?", default=False)


def _report_missing_packages(say: Callable[[str], None]) -> None:
    missing = []
    if not VirtualScreen.available():
        missing.append("pyte")
    if not sdk_available():
        missing.append("typesafe-sdk")
    if missing:
        say(f"\nStill needed: {', '.join(missing)}. Install with:\n  {INSTALL_HINT}")


def _confirm(ask: Callable[[str], str], question: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = ask(f"{question} {suffix} ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")
