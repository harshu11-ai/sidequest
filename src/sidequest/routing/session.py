"""Per-session glue: watch a prompt being submitted and switch models first."""

from __future__ import annotations

import re

from sidequest.lifecycle import ComposerTracker
from sidequest.routing.router import ModelRouter
from sidequest.routing.screen import VirtualScreen
from sidequest.routing.switcher import (
    ModelSwitcher,
    PromptNotRestored,
    PtyIO,
    SessionPreferences,
    SwitchError,
)

# Phrases an agent shows while it is working. Its input box then queues whatever
# is typed, so a `/model` typed then would be sent as a message instead.
_BUSY = re.compile(r"(?:esc|ctrl\+c) to interrupt|\bworking\b\s*\(", re.IGNORECASE)
# Enter as an agent receives it: CR, LF, or the Kitty keyboard-protocol Enter.
_ENTER_KEYS = (b"\r", b"\n", b"\x1b[13u")
# After this many failed switches in a row, stop trying for the session.
_MAX_FAILURES = 2


class RoutingSession:
    def __init__(
        self,
        application: str,
        router: ModelRouter,
        screen: VirtualScreen,
    ) -> None:
        self._application = application
        self._router = router
        self._screen = screen
        self._tracker = ComposerTracker()
        self._modes = SessionPreferences()
        self._failures = 0
        self.switches: list[str] = []
        self.notes: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._failures < _MAX_FAILURES

    def child_output(self, data: bytes) -> None:
        self._screen.feed(data)

    def resize(self, rows: int, columns: int) -> None:
        self._screen.resize(rows, columns)

    def intercept(self, child_input: bytes, io: PtyIO) -> bytes | None:
        """Handle a chunk headed to the agent that submits a prompt.

        Returns None when the chunk should be sent as usual. Otherwise the part
        before Enter has already been sent (with the model switched in between),
        and the returned bytes -- just the Enter -- are still to be sent.
        """
        self._tracker.feed(child_input)
        if not self.enabled or len(self._tracker.submitted) != 1:
            return None
        raw, cursor_at_end = self._tracker.submitted[0]
        enter = next((key for key in _ENTER_KEYS if child_input.endswith(key)), None)
        if enter is None or not cursor_at_end or not self._safe_to_type():
            return None

        model = self._router.decide(raw.decode("utf-8", "replace"))
        if model is None:
            return None

        head = child_input[: -len(enter)]
        if head:
            io.write(head)
        try:
            switcher = ModelSwitcher(self._application, self._screen, io, modes=self._modes)
            changed = switcher.switch(model, raw)
        except PromptNotRestored as error:
            # The switch itself took; only the prompt didn't come back.
            self._router.switched(model)
            self.switches.append(model)
            self.notes.append(f"switched to {model}, but {error}: retype your prompt")
            return b""  # nothing to submit: don't let Enter answer some other prompt
        except SwitchError as error:
            self._failures += 1
            self.notes.append(f"could not switch to {model}: {error}")
            if not self.enabled:
                self.notes.append("model routing turned itself off for this session")
        else:
            self._failures = 0
            self._router.switched(model)
            if changed:
                self.switches.append(model)
        return enter

    def _safe_to_type(self) -> bool:
        if not self._screen.bracketed_paste:
            return False  # the prompt could not be put back intact
        return _BUSY.search(self._screen.text()) is None
