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
        self._typed_length = 0

    def user_input(self, data: bytes) -> None:
        if not self._watch_codex_input or not self._codex_prompt_ready:
            return
        visible = _ANSI_SEQUENCE.sub(b"", data)
        for byte in visible:
            if byte in (0x0D, 0x0A):
                if self._typed_length > 0:
                    self.companion.show()
                self._typed_length = 0
            elif byte in (0x08, 0x7F):
                self._typed_length = max(0, self._typed_length - 1)
            elif byte >= 0x20:
                self._typed_length += 1
            # other control bytes (Tab, Ctrl+C, arrow-key remnants, ...) don't
            # count as typed content and don't clear it either.

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
