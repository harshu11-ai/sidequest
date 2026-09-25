"""Session-scoped lifecycle integration for Codex and Claude Code."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Protocol


class LifecycleCompanion(Protocol):
    """What AgentLifecycle needs from a sidequest companion (chess, videos, ...)."""

    def show(self) -> None: ...
    def hide(self) -> None: ...
    def lifecycle_url(self, event: str) -> str: ...


_CODEX_PROMPT_TEXT = b"Ask Codex to do anything"
_ANSI_SEQUENCE = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][0-2A-Z])"
)
_OSC9_SEQUENCE = re.compile(rb"\x1b\]9;.*?(?:\x07|\x1b\\)", re.DOTALL)
_TERMINAL_CONTROL = re.compile(rb"[\x00-\x08\x0b-\x1f\x7f]")


_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"
# What can follow a lone ESC and still belong to one escape sequence.
_SEQUENCE_CONTINUATIONS = b"[O]P_^X"
_MAX_STRING = 1024
_RECALLED = b"?"  # stands in for text an Up / Ctrl-P recall put in the box
_KITTY_MODIFIER_SHIFT, _KITTY_MODIFIER_ALT, _KITTY_MODIFIER_CTRL = 1, 2, 4
# Controls that move the cursor or edit away from the end: Ctrl-A/B/E/F/K/T/Y, Tab, Ctrl-L.
_CURSOR_MOVING_CONTROLS = {0x01, 0x02, 0x05, 0x06, 0x09, 0x0B, 0x0C, 0x14, 0x19}
# CSI finals for arrows (B, C, D), Home/End (H, F), and "CSI n ~" keys (Delete, PgUp, ...).
_CURSOR_MOVING_FINALS = {ord("B"), ord("C"), ord("D"), ord("H"), ord("F"), ord("~")}


class ComposerTracker:
    """Follow what is in an agent's prompt box, from the keystrokes sent to it.

    Only one question matters: when Enter arrives, is a prompt being sent?
    That needs the escape sequences understood (an arrow key is not text, a
    paste's newlines are not Enter, Shift+Enter and Alt+Enter are newlines) and
    the keys that empty or refill the box (Ctrl-C/U/W, Backspace, Up recall).
    """

    def __init__(self) -> None:
        self._line = bytearray()
        self._escape = bytearray()
        self._in_paste = False
        self._paste_tail = bytearray()
        # False once the cursor may be anywhere but the end of the box (arrow
        # keys, Home/End, Tab, ...) or text we never saw may be in it (recall).
        # The box then can't be emptied by backspacing, so callers that need to
        # rewrite it must leave it alone. Cleared again by emptying or submitting.
        self.at_end = True
        # (text, at_end) for each prompt submitted by the latest feed().
        self.submitted: list[tuple[bytes, bool]] = []

    @property
    def text(self) -> bytes:
        """What the box holds right now, newlines as LF."""
        return bytes(self._line)

    def feed(self, data: bytes) -> int:
        """Consume input; return how many prompts were submitted in it."""
        submitted = 0
        self.submitted = []
        self._resolve_lone_escape(data)
        for byte in data:
            if self._in_paste:
                self._paste_byte(byte)
            elif self._escape:
                submitted += self._escape_byte(byte)
            elif byte == 0x1B:
                self._escape.append(byte)
            else:
                submitted += self._key(byte)
        return submitted

    # -- keys -------------------------------------------------------------

    def _key(self, byte: int) -> int:
        if byte in (0x0D, 0x0A):
            return self._submit()
        if byte in (0x08, 0x7F):
            self._backspace()
        elif byte in (0x03, 0x15):  # Ctrl-C / Ctrl-U empty the box
            self._line.clear()
            self.at_end = True
        elif byte == 0x17:
            self._delete_word()
        elif byte == 0x10:  # Ctrl-P: previous history entry
            self._recall()
        elif byte >= 0x20:
            self._line.append(byte)
        elif byte in _CURSOR_MOVING_CONTROLS:
            self.at_end = False
        # other control bytes don't change what's in the box or where the cursor is
        return 0

    def _submit(self) -> int:
        raw = bytes(self._line)
        line = raw.lstrip()
        self._line.clear()
        at_end, self.at_end = self.at_end, True
        # A leading "/" is a built-in command (/model, /status, ...), not a
        # prompt handed to the agent -- nothing to take a break for.
        if not line or line.startswith(b"/"):
            return 0
        self.submitted.append((raw, at_end))
        return 1

    def _backspace(self) -> None:
        while self._line and self._line[-1] & 0xC0 == 0x80:  # UTF-8 continuation
            self._line.pop()
        del self._line[-1:]

    def _delete_word(self) -> None:
        while self._line and self._line[-1] in b" \n":
            self._line.pop()
        while self._line and self._line[-1] not in b" \n":
            self._line.pop()

    def _recall(self) -> None:
        self._line[:] = _RECALLED
        self.at_end = False

    # -- escape sequences -------------------------------------------------

    def _resolve_lone_escape(self, data: bytes) -> None:
        """An ESC ending the previous read, followed later by ordinary input, was Esc."""
        if self._escape != b"\x1b" or not data or self._in_paste:
            return
        if data[0] == 0x1B:
            continues = data[1:2] not in (b"[", b"O", b"]")
        else:
            continues = data[0] in _SEQUENCE_CONTINUATIONS
        if not continues:
            self._escape.clear()

    def _escape_byte(self, byte: int) -> int:
        escape = self._escape
        escape.append(byte)
        if len(escape) == 2:
            if byte in _SEQUENCE_CONTINUATIONS or byte == 0x1B:
                return 0
            escape.clear()
            self._meta(byte)
            return 0
        kind = escape[1]
        if kind == 0x1B:  # ESC ESC: legacy Alt+<sequence>, or two Esc presses
            escape.clear()
            if byte == ord("["):
                escape.extend(b"\x1b[")
                return 0
            return self._key(byte)
        if kind == ord("["):
            if 0x40 <= byte <= 0x7E:
                parameters = bytes(escape[2:-1])
                escape.clear()
                return self._csi(parameters, byte)
            if len(escape) >= 64:
                escape.clear()
        elif kind == ord("O"):
            escape.clear()
            self._csi(b"", byte)
        else:  # OSC / DCS / APC / PM / SOS string: ends with ST (OSC also BEL)
            ended = bytes(escape).endswith(b"\x1b\\") or (kind == ord("]") and byte == 0x07)
            if ended or byte in (0x0D, 0x0A) or len(escape) >= _MAX_STRING:
                escape.clear()
        return 0

    def _meta(self, byte: int) -> None:
        if byte in (0x0D, 0x0A):  # Alt+Enter: a newline inside the prompt
            self._line.append(0x0A)
        elif byte in (0x08, 0x7F):  # Alt+Backspace
            self._delete_word()
        else:  # Alt+B/F/D, ...: no text typed, but the cursor may have moved
            self.at_end = False

    def _csi(self, parameters: bytes, final: int) -> int:
        fields = parameters.split(b";")
        if final == ord("A") and not parameters.startswith((b"<", b"?", b">")):
            self._recall()  # Up, Alt+Up (pop the queue): text comes back into the box
        elif final == ord("~") and fields[0] == b"200":
            self._in_paste = True
            self._paste_tail.clear()
        elif final in _CURSOR_MOVING_FINALS and not parameters.startswith((b"<", b"?", b">", b"=")):
            self.at_end = False  # arrows, Home/End, Delete, Page Up/Down, ...
        elif final == ord("u"):
            return self._kitty_key(fields)
        return 0

    def _kitty_key(self, fields: list[bytes]) -> int:
        key = fields[0].split(b":")[0]
        detail = fields[1].split(b":") if len(fields) > 1 else [b"1"]
        if not key.isdigit() or not detail[0].isdigit():
            return 0
        if len(detail) > 1 and detail[1] == b"3":  # key release
            return 0
        code, modifiers = int(key), max(int(detail[0]) - 1, 0)
        if code == 13:
            if modifiers & (_KITTY_MODIFIER_SHIFT | _KITTY_MODIFIER_ALT | _KITTY_MODIFIER_CTRL):
                self._line.append(0x0A)  # Shift/Alt/Ctrl+Enter: a newline
                return 0
            return self._submit()
        if modifiers & _KITTY_MODIFIER_CTRL:
            if code in (ord("c"), ord("u")):
                self._line.clear()
            elif code == ord("w"):
                self._delete_word()
        return 0

    # -- bracketed paste --------------------------------------------------

    def _paste_byte(self, byte: int) -> None:
        tail = self._paste_tail
        tail.append(byte)
        while tail and not _PASTE_END.startswith(tail):
            del tail[0]
        if bytes(tail) == _PASTE_END:
            self._in_paste = False
            tail.clear()
        elif not tail:  # not part of the end marker: pasted text, newlines included
            self._line.append(0x0A if byte in (0x0D, 0x0A) else byte)


class AgentLifecycle:
    """Translate terminal activity and agent events into companion visibility."""

    def __init__(
        self,
        companion: LifecycleCompanion,
        *,
        watch_codex_input: bool = False,
    ) -> None:
        self.companion = companion
        self._watch_codex_input = watch_codex_input
        self._codex_prompt_ready = False
        self._output_tail = b""
        self._composer = ComposerTracker()

    def user_input(self, data: bytes) -> None:
        if not self._watch_codex_input or not self._codex_prompt_ready:
            return
        for _ in range(self._composer.feed(data)):
            self.companion.show()

    def child_output(self, data: bytes) -> None:
        combined = self._output_tail + data
        if self._watch_codex_input and _contains_codex_prompt(combined):
            self._codex_prompt_ready = True
        if _contains_osc9(combined):
            self.companion.hide()
            combined = _OSC9_SEQUENCE.sub(b"", combined)
        self._output_tail = combined[-2048:]


def prepare_agent_command(
    command: Sequence[str],
    application: str,
    companion: LifecycleCompanion,
) -> list[str]:
    """Add temporary lifecycle settings without writing user configuration."""
    prepared = list(command)
    if application == "codex":
        settings = [
            'tui.notifications=["agent-turn-complete","approval-requested"]',
            'tui.notification_method="osc9"',
            'tui.notification_condition="always"',
        ]
        insertion: list[str] = []
        for setting in settings:
            insertion.extend(["--config", setting])
        return [prepared[0], *insertion, *prepared[1:]]
    if application == "claude":
        settings = _claude_hook_settings(companion)
        encoded_settings = json.dumps(settings, separators=(",", ":"))
        return [prepared[0], "--settings", encoded_settings, *prepared[1:]]
    raise ValueError(f"unsupported application: {application}")


def _claude_hook_settings(companion: LifecycleCompanion) -> dict[str, object]:
    def hook(event: str) -> list[dict[str, object]]:
        return [
            {
                "hooks": [
                    {
                        "type": "http",
                        "url": companion.lifecycle_url(event),
                        "timeout": 5,
                    }
                ]
            }
        ]

    return {
        "hooks": {
            "UserPromptSubmit": hook("start"),
            "PostToolUse": hook("start"),
            "PostToolUseFailure": hook("start"),
            "PermissionRequest": hook("stop"),
            "Stop": hook("stop"),
            "StopFailure": hook("stop"),
        }
    }


def _contains_osc9(data: bytes) -> bool:
    return _OSC9_SEQUENCE.search(data) is not None


def _contains_codex_prompt(data: bytes) -> bool:
    """Recognize Codex's input placeholder despite its ANSI-heavy TUI redraws."""
    visible_text = _ANSI_SEQUENCE.sub(b"", data)
    visible_text = _TERMINAL_CONTROL.sub(b"", visible_text)
    compact_text = re.sub(rb"\s+", b"", visible_text)
    compact_prompt = re.sub(rb"\s+", b"", _CODEX_PROMPT_TEXT)
    return compact_prompt in compact_text
