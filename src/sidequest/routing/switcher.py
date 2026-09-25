"""Drive an agent's own /model picker to change its model for this session only.

Neither Claude Code nor Codex has a "switch model" API, so this types into the
picker the way a person would. Two rules keep it from touching the user's
saved settings: Enter and the number keys in the pickers save a *default for
new sessions*, so only the `s` ("this session only") key ever confirms a choice.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sidequest.routing.screen import Picker, VirtualScreen

BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"
_SESSION_ONLY = "for this session only"
# After the first turn Claude Code asks before switching: the new model has to
# re-read the whole (cached) conversation. Auto-routing means the user opted in.
_CACHE_DIALOG = "Switch model?"
_CACHE_YES = re.compile(r"^\s*❯\s*1\.\s*Yes\b")
_KEY_GAP_S = 0.15
_DOWN, _UP, _ESCAPE, _BACKSPACE, _SESSION_KEY = b"j", b"k", b"\x1b", b"\x7f", b"s"
_SHIFT_TAB = b"\x1b[Z"
_MODE_CYCLE_LIMIT = 5
_SETTLE_S = 0.4
_PASTE_WAIT_S = 1.5
# Long prompts may be shown collapsed ("[Pasted text +N lines]"), so only the
# start of a short one is looked for on screen.
_VERIFIABLE_CHARS = 400


def _squash(text: str) -> str:
    return " ".join(text.split())


def _snippet(prompt: bytes) -> str:
    """The start of *prompt* as it would look on screen, or "" if it can't be checked."""
    text = _squash(prompt.decode("utf-8", "replace"))
    return text[:30] if 0 < len(text) <= _VERIFIABLE_CHARS else ""


class PtyIO(Protocol):
    def write(self, data: bytes) -> None: ...

    def pump(self, seconds: float) -> None:
        """Forward the agent's output to the terminal and the screen for up to *seconds*."""
        ...


@dataclass(frozen=True, slots=True)
class SwitchTimeouts:
    open_picker: float = 2.5
    step: float = 1.5


class SwitchError(RuntimeError):
    """The picker did not behave as expected."""


class PromptNotRestored(SwitchError):
    """The model was switched, but the prompt could not be put back in the box."""


class SessionPreferences:
    """Remember what the user chose, so a model switch doesn't quietly change it.

    Claude Code: some models have no auto mode, so switching to one drops the
    permission mode to manual -- and it stays there after switching back.
    Codex: choosing a model resets reasoning effort to that model's default.
    A setting that differs from what our last switch left is the user's own choice.
    """

    def __init__(self) -> None:
        self.preferred: str | None = None  # permission mode
        self.left: str | None = None  # the mode our last switch left the footer in
        self.unavailable: set[str] = set()  # models where the preferred mode can't be had
        self.effort: str | None = None
        self.effort_left: str | None = None

    def observe(self, mode: str | None) -> None:
        if mode is not None and mode != self.left:
            self.preferred = mode

    def observe_effort(self, effort: str | None) -> None:
        if effort is not None and effort != self.effort_left:
            self.effort = effort


class ModelSwitcher:
    def __init__(
        self,
        application: str,
        screen: VirtualScreen,
        io: PtyIO,
        *,
        timeouts: SwitchTimeouts | None = None,
        modes: SessionPreferences | None = None,
    ) -> None:
        if application not in ("claude", "codex"):
            raise ValueError(f"unsupported application: {application}")
        self._modes = modes or SessionPreferences()
        self._application = application
        self._screen = screen
        self._io = io
        self._timeouts = timeouts or SwitchTimeouts()

    def switch(self, label: str, prompt: bytes) -> bool:
        """Change to the model *label* and leave *prompt* back in the input box.

        The prompt is already typed into the box when Enter is pressed, so it is
        cleared first and pasted back afterwards. Returns False if the agent was
        already on that model. Raises SwitchError if the picker misbehaves, after
        putting the prompt back.
        """
        self._modes.observe(self._screen.permission_mode())
        if self._application == "codex":  # Claude Code's own "● high · /effort" line looks alike
            self._modes.observe_effort(self._screen.reasoning_effort())
        self._note(
            f"mode before {self._screen.permission_mode()!r}, preferred {self._modes.preferred!r}"
        )
        self._clear_box(prompt)
        try:
            self._open_picker()
            changed = self._select(label)
            if changed:
                self._confirm()
                self._restore_permission_mode(label)
            else:
                self._close_pickers()
        except SwitchError:
            self._close_pickers()
            self._restore(prompt)
            raise
        self._modes.left = self._screen.permission_mode()
        if self._application == "codex":
            self._modes.effort_left = self._screen.reasoning_effort()
            self._note(
                f"effort left as {self._modes.effort_left!r}, preferred {self._modes.effort!r}"
            )
        self._note(f"mode left as {self._modes.left!r}, preferred {self._modes.preferred!r}")
        try:
            self._restore(prompt)
        except SwitchError as error:
            raise PromptNotRestored(str(error)) from error
        return changed

    # -- steps ------------------------------------------------------------

    def _clear_box(self, prompt: bytes) -> None:
        text = prompt.decode("utf-8", "replace")
        self._io.write(_BACKSPACE * len(text))
        self._pump(_KEY_GAP_S)

    def _open_picker(self) -> None:
        self._io.write(b"/model")
        self._pump(_KEY_GAP_S)
        self._io.write(b"\r")
        self._wait_for(lambda: self._screen.picker() is not None, self._timeouts.open_picker)

    def _select(self, label: str) -> bool:
        """Move the highlight to *label*; False if that is already the current model."""
        picker = self._require_picker()
        target = picker.find(label)
        if target is None:
            shown = ", ".join(entry.label for entry in picker.entries)
            raise SwitchError(f"model {label!r} is not in the picker ({shown})")
        if target.current:
            return False
        cursor = picker.cursor
        if cursor is None:
            raise SwitchError("could not tell which picker row is highlighted")
        steps = target.number - cursor.number
        for _ in range(abs(steps)):
            self._io.write(_DOWN if steps > 0 else _UP)
            self._pump(0.05)
        self._wait_for(lambda: self._highlighted(label), self._timeouts.step)
        return True

    def _confirm(self) -> None:
        before = self._confirmations()
        if self._application == "codex":
            # Codex asks for a reasoning level next; its Enter also saves a default.
            self._io.write(b"\r")
            self._wait_for(self._at_reasoning_level, self._timeouts.step)
            self._choose_effort()
        self._io.write(_SESSION_KEY)
        # Confirmations from earlier switches can still be on screen, so success
        # is a new one appearing once the picker (and any dialog) has gone.
        self._wait_for(
            lambda: self._answer_cache_dialog() or self._switched(before), self._timeouts.step
        )
        self._wait_for(lambda: self._switched(before), self._timeouts.step)

    def _choose_effort(self) -> None:
        """On Codex's reasoning screen, move to the effort the user already had."""
        wanted = self._modes.effort
        picker = self._screen.picker()
        target = picker.find(wanted) if picker and wanted else None
        cursor = picker.cursor if picker else None
        if target is None or cursor is None or target == cursor:
            return
        steps = target.number - cursor.number
        for _ in range(abs(steps)):
            self._io.write(_DOWN if steps > 0 else _UP)
            self._pump(0.05)
        self._wait_for(lambda: self._highlighted(target.label), self._timeouts.step)

    def _restore_permission_mode(self, label: str) -> None:
        """Cycle back to the user's permission mode, stopping if it isn't on offer."""
        wanted = self._modes.preferred
        if self._application != "claude" or wanted is None or label in self._modes.unavailable:
            return
        try:  # the footer is briefly blank while the agent redraws after a switch
            self._wait_for(lambda: self._screen.permission_mode() is not None, self._timeouts.step)
        except SwitchError:
            return
        start = self._screen.permission_mode()
        if start is None or start == wanted:
            return
        current = start
        for _ in range(_MODE_CYCLE_LIMIT):
            self._io.write(_SHIFT_TAB)
            previous = current
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                self._pump(0.05)
                current = self._screen.permission_mode() or previous
                if current != previous:
                    break
            if current == wanted:
                self._note(f"mode restored to {wanted!r}")
                return
            if current in (previous, start):
                break  # nothing changed, or a full cycle without finding it
        self._note(f"mode {wanted!r} not available on {label!r}; stopped at {current!r}")
        self._modes.unavailable.add(label)

    def _restore(self, prompt: bytes) -> None:
        """Put the prompt back in the box, and make sure it arrived.

        An agent still redrawing after the switch drops a paste that lands too
        soon, and a lost prompt is the one failure the user can't recover from.
        """
        self._pump(_SETTLE_S)
        if not prompt:
            return
        snippet = _snippet(prompt)
        for _attempt in range(2):
            self._io.write(BRACKETED_PASTE_START + prompt + BRACKETED_PASTE_END)
            if not snippet:
                self._pump(_KEY_GAP_S)
                return
            try:
                self._wait_for(lambda: snippet in _squash(self._screen.text()), _PASTE_WAIT_S)
            except SwitchError:
                continue
            self._pump(_KEY_GAP_S)
            return
        shown = " | ".join(line.strip() for line in self._screen.lines() if line.strip())
        self._note(f"prompt not seen after paste; screen: {shown[-900:]}")
        raise SwitchError("could not put the prompt back in the input box")

    def _close_pickers(self) -> None:
        # One Esc at a time: a second Esc on an empty Claude Code box opens its
        # rewind menu. Also clears "/model" if it never left the box.
        for _ in range(3):
            if self._screen.picker() is None:
                break
            self._io.write(_ESCAPE)
            self._pump(0.3)
        self._io.write(_BACKSPACE * len("/model"))
        self._pump(_KEY_GAP_S)

    # -- screen checks ----------------------------------------------------

    def _highlighted(self, label: str) -> bool:
        picker = self._screen.picker()
        cursor = picker.cursor if picker else None
        target = picker.find(label) if picker else None
        return cursor is not None and cursor == target

    def _confirmations(self) -> tuple[str, ...]:
        return tuple(line.strip() for line in self._screen.lines() if _SESSION_ONLY in line)

    def _switched(self, before: tuple[str, ...]) -> bool:
        return (
            self._screen.picker() is None
            and _CACHE_DIALOG not in self._screen.text()
            and self._confirmations() != before
        )

    def _answer_cache_dialog(self) -> bool:
        """Say yes to Claude Code's cache warning. True once it has been answered."""
        if _CACHE_DIALOG not in self._screen.text():
            return False
        if not any(_CACHE_YES.match(line) for line in self._screen.lines()):
            raise SwitchError("Claude Code's switch confirmation did not default to yes")
        self._io.write(b"\r")
        return True

    def _at_reasoning_level(self) -> bool:
        picker = self._screen.picker()
        return picker is not None and "reasoning" in picker.title.lower()

    def _require_picker(self) -> Picker:
        picker = self._screen.picker()
        if picker is None:
            raise SwitchError("the model picker did not open")
        return picker

    def _wait_for(self, condition: Callable[[], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            self._pump(0.05)
            if condition():
                return
            if time.monotonic() >= deadline:
                raise SwitchError("timed out waiting for the agent")

    def _pump(self, seconds: float) -> None:
        self._io.pump(seconds)

    def _note(self, text: str) -> None:
        note = getattr(self._io, "note", None)  # only the diagnostic log wants these
        if note is not None:
            note(text)
