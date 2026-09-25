"""A virtual terminal that shows what the wrapped agent currently has on screen."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_BRACKETED_PASTE = 2004
_CURSOR_MARKS = "❯›>"
_ROW = re.compile(
    rf"^\s*(?P<cursor>[{_CURSOR_MARKS}])?\s*[↓↑]?\s*(?P<number>\d{{1,2}})\.\s+(?P<rest>\S.*)$"
)
# Tags a picker appends to a model's name; they are not part of the name.
_TAGS = re.compile(r"\s*(?:\((?:current|default|recommended)\)|✔)\s*$")
_CURRENT = re.compile(r"\(current\)|✔")
_TITLE = re.compile(r"^\s*(Select\b.*?)\s*$")
_COLUMN_GAP = re.compile(r"\s{2,}")
# Claude Code's footer names its permission mode, e.g. "⏵⏵ auto mode on (shift+tab to cycle)".
# Codex's footer names model and reasoning effort, e.g. "GPT-6-Astra high · ~".
_EFFORT_FOOTER = re.compile(
    r"^\s*\S+\s+(?P<effort>low|medium|high|extra high|xhigh)\s+·", re.IGNORECASE
)
_MODE_FOOTER = re.compile(r"^\s*(?:⏵⏵|⏸)\s+(?P<mode>.+?)(?:\s+\(shift\+tab|\s+·|\s*$)")


@dataclass(frozen=True, slots=True)
class PickerEntry:
    number: int
    label: str
    highlighted: bool
    current: bool = False  # the model the agent is using right now


@dataclass(frozen=True, slots=True)
class Picker:
    title: str
    entries: tuple[PickerEntry, ...]

    @property
    def cursor(self) -> PickerEntry | None:
        return next((entry for entry in self.entries if entry.highlighted), None)

    def find(self, label: str) -> PickerEntry | None:
        wanted = _normalize(label)
        return next((e for e in self.entries if _normalize(e.label) == wanted), None)


def parse_picker(lines: list[str]) -> Picker | None:
    """Read a numbered "Select ..." list, as Claude Code and Codex draw them."""
    start = next((i for i, line in enumerate(lines) if _TITLE.match(line)), None)
    if start is None:
        return None
    entries: list[PickerEntry] = []
    for line in lines[start + 1 :]:
        match = _ROW.match(line)
        if match is None:
            if entries:
                break  # the list ended
            continue
        label = _COLUMN_GAP.split(match["rest"].strip(), maxsplit=1)[0]
        current = _CURRENT.search(label) is not None
        while (stripped := _TAGS.sub("", label)) != label:
            label = stripped
        highlighted = match["cursor"] is not None
        entries.append(PickerEntry(int(match["number"]), label, highlighted, current))
    if not entries:
        return None
    return Picker(_TITLE.match(lines[start])[1], tuple(entries))  # type: ignore[index]


def _normalize(label: str) -> str:
    return " ".join(label.lower().split())


class VirtualScreen:
    """Feed it the agent's output; read back the text on its screen."""

    def __init__(self, rows: int = 40, columns: int = 120) -> None:
        import pyte

        self._screen: Any = pyte.Screen(columns, rows)
        self._stream: Any = pyte.ByteStream(self._screen)

    @staticmethod
    def available() -> bool:
        try:
            import pyte  # noqa: F401
        except ImportError:
            return False
        return True

    def feed(self, data: bytes) -> None:
        self._stream.feed(data)

    def resize(self, rows: int, columns: int) -> None:
        self._screen.resize(rows, columns)

    def lines(self) -> list[str]:
        return [line.rstrip() for line in self._screen.display]

    def text(self) -> str:
        return "\n".join(self.lines())

    def picker(self) -> Picker | None:
        return parse_picker(self.lines())

    def reasoning_effort(self) -> str | None:
        """Codex's current reasoning effort as the picker words it ("high", "extra high")."""
        for line in reversed(self.lines()):
            match = _EFFORT_FOOTER.match(line)
            if match is not None:
                effort = match["effort"].lower()
                return "extra high" if effort == "xhigh" else effort
        return None

    def permission_mode(self) -> str | None:
        """Claude Code's current permission mode as its footer words it, if shown."""
        for line in reversed(self.lines()):
            match = _MODE_FOOTER.match(line)
            if match is not None:
                return match["mode"]
        return None

    @property
    def bracketed_paste(self) -> bool:
        """Whether the agent asked the terminal to bracket pasted text."""
        return (_BRACKETED_PASTE << 5) in self._screen.mode
