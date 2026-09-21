"""Scenario tests: what non-typing input does to autocorrect in Codex / Claude Code.

The unit tests in test_input_processor.py pin individual byte sequences. These
tests instead ask the question that matters to a user: *after* Tab, Esc,
Shift+Enter, a paste, a mouse wheel tick, a terminal report, ... does the
child's prompt box still end up correct?

The check is end to end. LineEditor is a small readline-style model of what an
agent's prompt box does with the bytes it receives. The same keystrokes are run
through it twice -- raw, and through InputProcessor -- and the results are
compared:

* safety:   the two results may differ only by whole-word corrections. Anything
            else means the processor corrupted the prompt.
* liveness: after an event that doesn't change the prompt text, the next typo
            should still be corrected (see `assert_recovers`).

Tests decorated with `known_bug` document a confirmed defect. They pass today
(as expected failures) and will start failing as "unexpected success" once the
defect is fixed -- delete the decorator then.
"""

from __future__ import annotations

import codecs
import os
import random
import re
import unittest
from collections.abc import Iterable, Sequence
from unittest.mock import Mock

from sidequest.autocorrect.corrector import ConservativeCorrector
from sidequest.autocorrect.input_processor import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    InputProcessor,
)
from sidequest.lifecycle import AgentLifecycle

ESC = b"\x1b"
CORRECTOR = ConservativeCorrector()


GLUE = (
    "typing glued onto text the tracker never saw is judged as a whole word "
    "(trade-off with test_arrow_key_with_nothing_in_flight_does_not_suspend_correction)"
)


def known_bug(reason: str):
    """Mark a test that documents a confirmed, not-yet-fixed defect."""

    def decorate(test):
        test.__doc__ = f"KNOWN BUG: {reason}\n\n{test.__doc__ or ''}".rstrip()
        if os.environ.get("SHOW_KNOWN_BUGS"):
            return test  # SHOW_KNOWN_BUGS=1 pytest ... prints the real failures
        return unittest.expectedFailure(test)

    return decorate


# --------------------------------------------------------------------------
# The child's side: a readline-style prompt box
# --------------------------------------------------------------------------


class LineEditor:
    """Just enough of an agent prompt box to tell whether text was corrupted.

    `completions` maps the word left of the cursor to its Tab completion.
    `recall` is what Up / Alt+Up load into the box (history or a queued message).
    """

    def __init__(
        self,
        *,
        completions: dict[str, str] | None = None,
        recall: Sequence[str] = (),
        bare_escape_at_read_end: bool = False,
    ) -> None:
        self.chars: list[str] = []
        self.cursor = 0
        self.submitted: list[str] = []
        self.completions = completions or {}
        self.recall = list(recall)
        # Real apps treat an ESC that ends a read as the Esc key, because a
        # terminal delivers a whole escape sequence in one write. Tests that
        # split sequences into single bytes leave this off.
        self._bare_escape_at_read_end = bare_escape_at_read_end
        self._state = "ground"
        self._params = bytearray()
        self._alt = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._in_paste = False
        self._paste_tail = bytearray()
        self._string_prev = 0

    # -- public ---------------------------------------------------------

    def write(self, data: bytes) -> None:
        for byte in data:
            self._byte(byte)
        if self._bare_escape_at_read_end and self._state in {"esc", "esc_esc"}:
            self._state = "ground"

    def lines(self) -> list[str]:
        return [*self.submitted, "".join(self.chars)]

    # -- byte level -------------------------------------------------------

    def _byte(self, byte: int) -> None:
        if self._in_paste:
            self._paste_byte(byte)
            return
        state = self._state
        if state == "ground":
            if byte == 0x1B:
                self._state = "esc"
            else:
                self._ground(byte)
        elif state == "esc":
            self._after_escape(byte)
        elif state == "esc_esc":
            if byte == ord("["):
                self._state, self._params, self._alt = "csi", bytearray(), True
            else:
                self._state = "ground"
                self._byte(byte)
        elif state == "csi":
            if 0x40 <= byte <= 0x7E:
                self._state = "ground"
                self._csi(bytes(self._params), byte)
                self._alt = False
            else:
                self._params.append(byte)
        elif state == "ss3":
            self._state = "ground"
            self._csi(b"", byte)
        elif state in {"osc", "dcs"}:
            ended = (state == "osc" and byte == 0x07) or (
                self._string_prev == 0x1B and byte == ord("\\")
            )
            self._string_prev = byte
            if ended:
                self._state = "ground"

    def _after_escape(self, byte: int) -> None:
        self._state = "ground"
        if byte == ord("["):
            self._state, self._params, self._alt = "csi", bytearray(), False
        elif byte == ord("O"):
            self._state = "ss3"
        elif byte == ord("]"):
            self._state, self._string_prev = "osc", 0
        elif byte in (ord("P"), ord("_")):
            self._state, self._string_prev = "dcs", 0
        elif byte == 0x1B:
            self._state = "esc_esc"
        else:
            self._meta(byte)

    def _paste_byte(self, byte: int) -> None:
        self._paste_tail.append(byte)
        del self._paste_tail[: -len(BRACKETED_PASTE_END)]
        if bytes(self._paste_tail) == BRACKETED_PASTE_END:
            self._in_paste = False
            # The end marker's bytes were inserted as text below; take them back.
            del self.chars[self.cursor - (len(BRACKETED_PASTE_END) - 1) : self.cursor]
            self.cursor -= len(BRACKETED_PASTE_END) - 1
            return
        self._insert(chr(byte) if byte in (0x0A, 0x0D) or byte < 0x80 else self._decode(byte))

    # -- keys -----------------------------------------------------------

    def _ground(self, byte: int) -> None:
        if byte == 0x0D:
            self.submitted.append("".join(self.chars))
            self.chars, self.cursor = [], 0
        elif byte == 0x0A:
            self._insert("\n")  # Ctrl-J: newline in the box
        elif byte in (0x7F, 0x08):
            if self.cursor:
                del self.chars[self.cursor - 1]
                self.cursor -= 1
        elif byte == 0x09:
            self._tab()
        elif byte == 0x01:
            self.cursor = 0
        elif byte == 0x05:
            self.cursor = len(self.chars)
        elif byte == 0x02:
            self.cursor = max(0, self.cursor - 1)
        elif byte == 0x06:
            self.cursor = min(len(self.chars), self.cursor + 1)
        elif byte == 0x0B:
            del self.chars[self.cursor :]
        elif byte == 0x15:
            del self.chars[: self.cursor]
            self.cursor = 0
        elif byte == 0x17:
            self._delete_word_back()
        elif byte == 0x03:
            self.chars, self.cursor = [], 0
        elif byte == 0x04:
            del self.chars[self.cursor : self.cursor + 1]
        elif byte == 0x10:
            self._recall()
        elif byte == 0x16:
            self._insert_text("[Image #1]")
        elif byte < 0x20:
            pass  # Ctrl-L, Ctrl-G, Ctrl-O, ... : no change to the text
        else:
            text = self._decode(byte)
            if text:
                self._insert(text)

    def _decode(self, byte: int) -> str:
        return self._decoder.decode(bytes([byte]))

    def _insert(self, text: str) -> None:
        for char in text:
            self.chars.insert(self.cursor, char)
            self.cursor += 1

    _insert_text = _insert

    def _tab(self) -> None:
        start = self.cursor
        while start and self.chars[start - 1] not in " \n":
            start -= 1
        word = "".join(self.chars[start : self.cursor])
        completion = self.completions.get(word)
        if completion is not None:
            self.chars[start : self.cursor] = list(completion)
            self.cursor = start + len(completion)

    def _recall(self) -> None:
        if self.recall:
            self.chars = list(self.recall.pop(0))
            self.cursor = len(self.chars)

    def _word_left(self) -> int:
        index = self.cursor
        while index and self.chars[index - 1] in " \n":
            index -= 1
        while index and self.chars[index - 1] not in " \n":
            index -= 1
        return index

    def _delete_word_back(self) -> None:
        start = self._word_left()
        del self.chars[start : self.cursor]
        self.cursor = start

    def _word_right(self) -> int:
        index = self.cursor
        while index < len(self.chars) and self.chars[index] in " \n":
            index += 1
        while index < len(self.chars) and self.chars[index] not in " \n":
            index += 1
        return index

    def _meta(self, byte: int) -> None:
        if byte == ord("b"):
            self.cursor = self._word_left()
        elif byte == ord("f"):
            self.cursor = self._word_right()
        elif byte == ord("d"):
            end = self._word_right()
            del self.chars[self.cursor : end]
        elif byte in (0x7F, 0x08):
            self._delete_word_back()
        elif byte == 0x0D:
            self._insert("\n")  # Alt+Enter: newline in the box

    def _csi(self, params: bytes, final: int) -> None:
        text = params.decode("ascii", "replace")
        if text[:1] in {"<", "?", ">"}:
            return  # mouse events and terminal replies never edit the box
        fields = text.split(";")
        modifier = int(fields[1].split(":")[0] or 1) - 1 if len(fields) > 1 else 0
        if self._alt:
            modifier |= 2
        letter = chr(final)
        if letter == "~":
            self._csi_tilde(fields[0])
        elif letter == "u":
            self._csi_u(fields[0].split(":")[0], modifier, text)
        elif letter == "A":
            self._recall()
        elif letter in "CD":
            word = modifier & 6  # Alt or Ctrl
            if letter == "D":
                self.cursor = self._word_left() if word else max(0, self.cursor - 1)
            else:
                self.cursor = self._word_right() if word else min(len(self.chars), self.cursor + 1)
        elif letter == "H":
            self.cursor = 0
        elif letter == "F":
            self.cursor = len(self.chars)
        # B, I, O, R, c, n, t, y, ... : nothing to do with the text

    def _csi_tilde(self, number: str) -> None:
        if number == "200":
            self._in_paste = True
            self._paste_tail.clear()
        elif number in {"1", "7"}:
            self.cursor = 0
        elif number in {"4", "8"}:
            self.cursor = len(self.chars)
        elif number == "3":
            del self.chars[self.cursor : self.cursor + 1]

    def _csi_u(self, key: str, modifier: int, raw: str) -> None:
        if ":3" in raw.split(";")[-1] and ";" in raw:
            return  # key release
        code = int(key or 0)
        if code == 13:
            if modifier & 3:  # Shift+Enter / Alt+Enter
                self._insert("\n")
            else:
                self._ground(0x0D)
        elif code == 99 and modifier & 4:
            self.chars, self.cursor = [], 0
        elif code in (127, 8) and modifier & 6:
            self._delete_word_back()


# --------------------------------------------------------------------------
# Running scripts and judging the result
# --------------------------------------------------------------------------


def run_raw(chunks: Iterable[bytes], **editor: object) -> list[str]:
    child = LineEditor(**editor)  # type: ignore[arg-type]
    for chunk in chunks:
        child.write(chunk)
    return child.lines()


def run_processed(
    chunks: Iterable[bytes], **editor: object
) -> tuple[list[str], InputProcessor, list[bytes]]:
    child = LineEditor(**editor)  # type: ignore[arg-type]
    processor = InputProcessor(CORRECTOR)
    sent: list[bytes] = []
    for chunk in chunks:
        out = processor.feed(chunk)
        sent.append(out)
        child.write(out)
    return child.lines(), processor, sent


# ESC not followed by [ O ] P _ ^ X is a legacy Meta key (Alt+b, Alt+Enter, ...).
# A terminal writes it in one piece, and an ESC that ends a read is read as the
# Esc key, so splitting one of these across reads describes a different
# keystroke. Scripts containing them are only meaningful in a single read.
_LEGACY_META = re.compile(rb"\x1b(?![\[O\]P_^X])")


def one_byte_at_a_time(data: bytes) -> list[bytes]:
    return [bytes([byte]) for byte in data]


def random_split(data: bytes, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    chunks, index = [], 0
    while index < len(data):
        size = rng.randint(1, 6)
        chunks.append(data[index : index + size])
        index += size
    return chunks


class ScenarioCase(unittest.TestCase):
    def assert_only_word_corrections(
        self, raw: list[str], out: list[str], script: object = ""
    ) -> None:
        """Processed text may differ from raw only by whole-token corrections."""
        note = f"\nscript: {script!r}\nraw:    {raw!r}\nout:    {out!r}"
        self.assertEqual(len(raw), len(out), "submitted line count changed" + note)
        for raw_line, out_line in zip(raw, out, strict=True):
            raw_tokens = re.split(r"([ \n])", raw_line)
            out_tokens = re.split(r"([ \n])", out_line)
            self.assertEqual(len(raw_tokens), len(out_tokens), "spacing changed" + note)
            for before, after in zip(raw_tokens, out_tokens, strict=True):
                if before == after:
                    continue
                fix = CORRECTOR.suggest(before)
                self.assertTrue(
                    fix is not None and fix.replacement == after,
                    f"{before!r} became {after!r}, which is not a correction" + note,
                )

    def assert_safe(self, chunks: Sequence[bytes], **editor: object) -> list[str]:
        raw = run_raw(chunks, **editor)
        out, _, _ = run_processed(chunks, **editor)
        self.assert_only_word_corrections(raw, out, chunks)
        return out

    def assert_safe_however_chunked(self, data: bytes, **editor: object) -> list[str]:
        """Same script, whole and split up: safe, and chunking must not matter."""
        variants = [[data]]
        if not _LEGACY_META.search(data):
            variants += [
                one_byte_at_a_time(data),
                random_split(data, 1),
                random_split(data, 2),
            ]
        results = [self.assert_safe(chunks, **editor) for chunks in variants]
        for other in results[1:]:
            self.assertEqual(results[0], other, f"chunking changed the result for {data!r}")
        return results[0]

    def assert_recovers(self, before: bytes, *, splits: bool = True, **editor: object) -> None:
        """After `before`, a later typo (adn) is still corrected to `and`."""
        data = before + b" adn "
        variants = [[data]]
        if splits and not _LEGACY_META.search(data):
            variants += [one_byte_at_a_time(data), random_split(data, 7)]
        for chunks in variants:
            out = self.assert_safe(chunks, **editor)
            text = "\n".join(out)
            self.assertIn(" and ", text + " ", f"typo not corrected after {before!r}: {out!r}")
            self.assertNotIn("adn", text, f"typo left behind after {before!r}: {out!r}")

    def assert_suspended(self, before: bytes, **editor: object) -> None:
        """Documented, deliberate: the typo after `before` is left alone (not corrupted)."""
        out = self.assert_safe([before + b" adn "], **editor)
        self.assertIn("adn", "\n".join(out))


# --------------------------------------------------------------------------
# The simulator itself has to be right or nothing below means anything
# --------------------------------------------------------------------------


class LineEditorModelTests(unittest.TestCase):
    def test_types_edits_and_submits(self) -> None:
        self.assertEqual(run_raw([b"abc\x7f\x7fx\r", b"next"]), ["ax", "next"])

    def test_cursor_and_home_end(self) -> None:
        self.assertEqual(run_raw([b"world\x1b[H", b"hello ", b"\x1b[F!"]), ["hello world!"])

    def test_tab_completes_the_word_left_of_the_cursor(self) -> None:
        self.assertEqual(
            run_raw([b"open src/si\t"], completions={"src/si": "src/sidequest/"}),
            ["open src/sidequest/"],
        )

    def test_up_recalls_history(self) -> None:
        self.assertEqual(run_raw([b"\x1b[A more"], recall=["fix it"]), ["fix it more"])

    def test_shift_enter_and_alt_enter_insert_a_newline_without_submitting(self) -> None:
        for newline in (b"\x1b[13;2u", b"\x1b\r", b"\n", b"\x1b[27;2;13~"):
            with self.subTest(newline=newline):
                out = run_raw([b"a" + newline + b"b"])
                # modifyOtherKeys form isn't modelled by the box; the rest are.
                if newline != b"\x1b[27;2;13~":
                    self.assertEqual(out, ["a\nb"])

    def test_paste_inserts_text_including_newlines(self) -> None:
        pasted = BRACKETED_PASTE_START + b"one\ntwo\rthree" + BRACKETED_PASTE_END
        self.assertEqual(run_raw([pasted + b"!"]), ["one\ntwo\rthree!"])

    def test_terminal_replies_and_mouse_never_touch_the_text(self) -> None:
        noise = (
            b"\x1b[I\x1b[12;40R\x1b[?1;2c\x1b[?2026;2$y\x1b]11;rgb:1e/1e/1e\x07"
            b"\x1bP>|term 1\x1b\\\x1b_Gi=1;OK\x1b\\\x1b[<64;1;1M\x1b[<0;1;1m"
        )
        self.assertEqual(run_raw([b"ab" + noise + b"cd"]), ["abcd"])

    def test_utf8_is_decoded_across_writes(self) -> None:
        self.assertEqual(run_raw([b"caf\xc3", b"\xa9!"]), ["café!"])


# --------------------------------------------------------------------------
# Sanity: plain typing through the simulator
# --------------------------------------------------------------------------


class BaselineTests(ScenarioCase):
    def test_typo_is_corrected_and_only_the_typo(self) -> None:
        out = self.assert_safe_however_chunked(b"please fix teh function adn go ")
        self.assertEqual(out, ["please fix the function and go "])

    def test_typo_is_corrected_on_submit_and_enter_is_not_lost(self) -> None:
        out = self.assert_safe_however_chunked(b"fix teh\rnext adn\r")
        self.assertEqual(out, ["fix the", "next and", ""])


# --------------------------------------------------------------------------
# Byte catalogs
# --------------------------------------------------------------------------

PASTE = lambda text: BRACKETED_PASTE_START + text + BRACKETED_PASTE_END  # noqa: E731

# Things the terminal or mouse send that can never change the prompt text or
# where the cursor is. Correction must carry on straight through them.
TRANSPARENT = {
    "focus in": b"\x1b[I",
    "focus out": b"\x1b[O",
    "cursor position report": b"\x1b[12;40R",
    "primary device attributes": b"\x1b[?1;2c",
    "secondary device attributes": b"\x1b[>1;95;0c",
    "kitty keyboard flags reply": b"\x1b[?0u",
    "window size report": b"\x1b[8;40;120t",
    "colour scheme report": b"\x1b[?997;1n",
    "OSC 11 reply (BEL)": b"\x1b]11;rgb:1e1e/1e1e/1e1e\x07",
    "OSC 11 reply (ST)": b"\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\",
    "mouse motion": b"\x1b[<35;80;24M",
    "shift+tab": b"\x1b[Z",
    "kitty shift+tab": b"\x1b[9;2u",
}
# Same guarantee, but these are not recognised today.
DECRPM_REPLIES = {
    "DECRPM synchronized output": b"\x1b[?2026;2$y",
    "DECRPM bracketed paste": b"\x1b[?2004;1$y",
}
STRING_REPLIES = {
    "DCS XTVERSION reply": b"\x1bP>|iTerm2 3.5.0\x1b\\",
    "APC kitty graphics reply": b"\x1b_Gi=31;OK\x1b\\",
}
MOUSE_WHEEL_AND_RELEASE = {
    "wheel up": b"\x1b[<64;80;24M",
    "wheel down": b"\x1b[<65;80;24M",
    "shift+wheel": b"\x1b[<68;80;24M",
    "button release": b"\x1b[<0;80;24m",
}

HOME_END = {
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "SS3 home": b"\x1bOH",
    "SS3 end": b"\x1bOF",
    "home (1~)": b"\x1b[1~",
    "end (4~)": b"\x1b[4~",
    "home (7~)": b"\x1b[7~",
    "end (8~)": b"\x1b[8~",
    "ctrl+a": b"\x01",
    "ctrl+e": b"\x05",
}
WORD_JUMPS = {
    "ctrl+left": b"\x1b[1;5D",
    "ctrl+right": b"\x1b[1;5C",
    "alt+left": b"\x1b[1;3D",
    "alt+right": b"\x1b[1;3C",
    "shift+left": b"\x1b[1;2D",
    "alt+b": b"\x1bb",
    "alt+f": b"\x1bf",
}
PLAIN_ARROWS = {
    "left": b"\x1b[D",
    "right": b"\x1b[C",
    "SS3 left": b"\x1bOD",
    "SS3 right": b"\x1bOC",
}
EVERYTHING_ELSE = {
    "page up": b"\x1b[5~",
    "page down": b"\x1b[6~",
    "delete": b"\x1b[3~",
    "ctrl+b": b"\x02",
    "ctrl+f": b"\x06",
    "ctrl+k": b"\x0b",
    "ctrl+l": b"\x0c",
    "ctrl+u": b"\x15",
    "ctrl+y": b"\x19",
    "ctrl+v (image paste)": b"\x16",
    "ctrl+g": b"\x07",
    "ctrl+o": b"\x0f",
    "ctrl+r": b"\x12",
    "ctrl+t": b"\x14",
    "ctrl+z": b"\x1a",
    "alt+d": b"\x1bd",
    "alt+backspace": b"\x1b\x7f",
    "mouse click": b"\x1b[<0;80;24M",
}
NEWLINE_KEYS_HANDLED = {
    "ctrl+j": b"\n",
    "backslash then enter": b"\\\r",
}
NEWLINE_KEYS_MISSED = {
    "kitty shift+enter": b"\x1b[13;2u",
    "alt+enter (legacy)": b"\x1b\r",
    "modifyOtherKeys shift+enter": b"\x1b[27;2;13~",
}
NON_ASCII = {
    "accented letter": "é".encode(),
    "emoji": "😀".encode(),
    "em dash": "—".encode(),
    "CJK": "日本".encode(),
    "curly apostrophe": "’".encode(),
}

EVERY_KEY = {
    **TRANSPARENT,
    **DECRPM_REPLIES,
    **STRING_REPLIES,
    **MOUSE_WHEEL_AND_RELEASE,
    **HOME_END,
    **WORD_JUMPS,
    **PLAIN_ARROWS,
    **EVERYTHING_ELSE,
    **NEWLINE_KEYS_HANDLED,
    **NEWLINE_KEYS_MISSED,
    **NON_ASCII,
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "tab": b"\t",
    "kitty escape": b"\x1b[27u",
    "alt+up (kitty)": b"\x1b[1;3A",
    "alt+up (legacy)": b"\x1b\x1b[A",
    "kitty ctrl+c press+release": b"\x1b[99;5u\x1b[99;5:3u",
}
COMPLETIONS = {"fix": "fixture ", "x": "xylophone", "ab": "abc", "/mo": "/model "}
RECALL = ["fix it", "again"]


class SafetyEverywhereTests(ScenarioCase):
    """No key, anywhere in a line, may corrupt what the user is typing."""

    CONTEXTS = (
        ("between words", b"fix ", b" adn teh "),
        ("mid word", b"fix te", b"h adn "),
        ("right after a word", b"fix teh", b" adn "),
        ("start of line", b"", b"teh adn "),
        ("after a submit", b"fix teh\r", b"adn "),
    )

    def test_every_key_in_every_position(self) -> None:
        for name, key in EVERY_KEY.items():
            for where, before, after in self.CONTEXTS:
                if name == "up" and not after.startswith((b" ", b"h")):
                    # Up loads recalled text, so typing glued straight onto it
                    # is a different bug: see QueueAndHistoryTests.
                    continue
                with self.subTest(key=name, where=where):
                    self.assert_safe_however_chunked(
                        before + key + after, completions=COMPLETIONS, recall=list(RECALL)
                    )

    def test_two_different_keys_back_to_back(self) -> None:
        names = list(EVERY_KEY)
        rng = random.Random(5)
        for _ in range(400):
            first, second = rng.choice(names), rng.choice(names)
            with self.subTest(first=first, second=second):
                self.assert_safe(
                    # The space after the second key keeps this about key
                    # interplay; typing glued to text is covered separately.
                    [b"fix te" + EVERY_KEY[first] + b"h " + EVERY_KEY[second] + b" adn teh "],
                    completions=COMPLETIONS,
                    recall=list(RECALL),
                )


class TerminalRepliesTests(ScenarioCase):
    """Replies and mouse noise arrive at any moment, including mid-word."""

    POSITIONS = (
        ("between words", b"fix ", b"adn teh "),
        ("mid word", b"fix te", b"h adn "),
        ("right after a word", b"fix teh", b" adn "),
        ("before a word", b"fix ", b"teh "),
    )

    def assert_transparent(self, event: bytes) -> None:
        for where, before, after in self.POSITIONS:
            baseline, _, _ = run_processed([before + after])
            for chunks in (
                [before + event + after],
                one_byte_at_a_time(before + event + after),
                random_split(before + event + after, 3),
            ):
                self.assertEqual(
                    self.assert_safe(chunks),
                    baseline,
                    f"{event!r} {where} changed what was corrected",
                )

    def test_handled_replies_never_interrupt_correction(self) -> None:
        for name, event in TRANSPARENT.items():
            with self.subTest(event=name):
                self.assert_transparent(event)

    def test_decrpm_replies_never_interrupt_correction(self) -> None:
        for name, event in DECRPM_REPLIES.items():
            with self.subTest(event=name):
                self.assert_transparent(event)

    def test_dcs_and_apc_replies_never_interrupt_correction(self) -> None:
        for name, event in STRING_REPLIES.items():
            with self.subTest(event=name):
                self.assert_transparent(event)

    def test_mouse_wheel_and_release_never_interrupt_correction(self) -> None:
        for name, event in MOUSE_WHEEL_AND_RELEASE.items():
            with self.subTest(event=name):
                self.assert_transparent(event)

    def test_startup_replies_do_not_cost_the_first_prompt_its_correction(self) -> None:
        startup = b"".join(
            TRANSPARENT[name]
            for name in (
                "primary device attributes",
                "kitty keyboard flags reply",
                "OSC 11 reply (BEL)",
                "focus in",
            )
        )
        self.assert_recovers(startup + b"fix teh")

    def test_terminal_queries_answered_at_startup_do_not_cost_the_first_prompt(self) -> None:
        startup = (
            DECRPM_REPLIES["DECRPM synchronized output"]
            + STRING_REPLIES["DCS XTVERSION reply"]
            + TRANSPARENT["primary device attributes"]
        )
        self.assert_recovers(startup + b"fix teh")


class CursorMovementTests(ScenarioCase):
    def test_plain_arrows_with_nothing_in_flight_keep_correction_alive(self) -> None:
        for name, key in PLAIN_ARROWS.items():
            with self.subTest(key=name):
                self.assert_recovers(b"fix " + key)

    def test_mid_word_movement_suspends_only_that_line_and_never_corrupts(self) -> None:
        for name, key in {**PLAIN_ARROWS, **HOME_END, **WORD_JUMPS}.items():
            with self.subTest(key=name):
                out = self.assert_safe([b"fix te" + key + b"h adn "])
                self.assertIn("adn", "\n".join(out), "word was interrupted; left alone")
                # A fresh line is always correctable again.
                self.assert_recovers(b"fix te" + key + b"h\r")

    @known_bug(GLUE)
    def test_typing_glued_to_text_after_moving_the_cursor_left(self) -> None:
        # "fix " then Left puts the cursor between "fix" and the space, so
        # "adn" is really the tail of "fixadn".
        out = self.assert_safe([b"fix ", b"\x1b[D", b"adn "])
        self.assertEqual(out, ["fixadn  "])

    def test_home_and_end_keys_keep_correction_alive(self) -> None:
        for name, key in HOME_END.items():
            with self.subTest(key=name):
                self.assert_recovers(b"fix " + key)

    def test_word_jump_keys_keep_correction_alive(self) -> None:
        for name, key in WORD_JUMPS.items():
            with self.subTest(key=name):
                self.assert_recovers(b"fix " + key)


class MultilineInputTests(ScenarioCase):
    """Shift+Enter / Alt+Enter start a fresh line inside the same prompt."""

    def test_keys_that_already_reset_the_line(self) -> None:
        for name, key in NEWLINE_KEYS_HANDLED.items():
            with self.subTest(key=name):
                self.assert_recovers(b"fix teh" + key)

    def test_shift_enter_and_alt_enter_start_a_correctable_line(self) -> None:
        for name, key in NEWLINE_KEYS_MISSED.items():
            with self.subTest(key=name):
                self.assert_recovers(b"fix teh" + key)

    def test_kitty_encoded_enter_resets_the_line(self) -> None:
        # Only in terminals running the kitty protocol's "report all keys" mode.
        self.assert_recovers(b"fix \x1bq" + b"\x1b[13u")

    def test_correction_before_a_multiline_newline_is_not_lost(self) -> None:
        out = self.assert_safe([b"fix teh\nnext adn\r"])
        self.assertEqual(out, ["fix the\nnext and", ""])


class EscapeKeyTests(ScenarioCase):
    """Esc interrupts the agent and closes menus; it never edits the prompt."""

    def test_kitty_encoded_escape_is_harmless(self) -> None:
        self.assert_recovers(b"fix teh\x1b[27u")

    def test_bare_escape_between_words(self) -> None:
        chunks = [b"fix ", b"\x1b", b"adn "]
        out = self.assert_safe(chunks, bare_escape_at_read_end=True)
        self.assertEqual(out, ["fix and "])

    def test_bare_escape_mid_word(self) -> None:
        chunks = [b"fix te", b"\x1b", b"h adn "]
        out = self.assert_safe(chunks, bare_escape_at_read_end=True)
        self.assertIn(" and ", "\n".join(out) + " ")

    def test_bare_escape_to_interrupt_the_agent_then_next_prompt(self) -> None:
        # Esc to stop a run, then type the follow-up prompt.
        chunks = [b"\x1b", b"adn fix teh "]
        out = self.assert_safe(chunks, bare_escape_at_read_end=True)
        self.assertEqual(out, ["and fix the "])

    def test_alt_key_in_one_read_is_deliberately_left_alone(self) -> None:
        # ESC + letter in the *same* read is a Meta key, which we can't model.
        self.assert_suspended(b"fix \x1bx")


class NonAsciiTests(ScenarioCase):
    def test_typing_around_non_ascii_is_never_corrupted(self) -> None:
        for name, char in NON_ASCII.items():
            for script in (
                b"hi " + char + b" adn teh ",
                b"caf" + char + b" adn ",
                char + b"\x7f adn ",
                b"adn " + char + b"\x7f\x7f adn ",
            ):
                with self.subTest(char=name, script=script):
                    self.assert_safe_however_chunked(script)

    def test_multibyte_character_split_across_reads(self) -> None:
        emoji = "😀".encode()
        for cut in range(1, len(emoji)):
            with self.subTest(cut=cut):
                self.assert_safe([b"hi " + emoji[:cut], emoji[cut:] + b" adn "])

    def test_words_after_a_non_ascii_character_are_still_corrected(self) -> None:
        for name, char in NON_ASCII.items():
            with self.subTest(char=name):
                self.assert_recovers(b"hi " + char)


class BackspaceTests(ScenarioCase):
    def test_retyping_a_word_after_deleting_it(self) -> None:
        out = self.assert_safe_however_chunked(b"fix teh\x7f\x7f\x7fadn ")
        self.assertEqual(out, ["fix and "])

    def test_backspace_inside_a_word_keeps_tracking_it(self) -> None:
        out = self.assert_safe_however_chunked(b"tehh\x7f ")
        self.assertEqual(out, ["the "])

    def test_backspace_after_a_correction_undoes_it(self) -> None:
        out = self.assert_safe([b"fix teh ", b"\x7f"])
        self.assertEqual(out, ["fix teh"])

    def test_erasing_the_space_and_finishing_the_same_word(self) -> None:
        # "teh" + space: corrected; user then backspaces twice (undo, then the
        # last letter) and completes a different word.
        out = self.assert_safe([b"teh ", b"\x7f", b"\x7f", b"m "])
        self.assertEqual(out, ["tem "])

    def test_backspacing_into_the_previous_word_and_typing_on(self) -> None:
        # "x" + space, backspace the space, type "teh": the word is "xteh".
        out = self.assert_safe([b"x ", b"\x7f", b"teh "])
        self.assertEqual(out, ["xteh "])

    def test_backspacing_over_several_characters_into_the_previous_word(self) -> None:
        out = self.assert_safe([b"ab cd", b"\x7f\x7f\x7f", b"teh "])
        self.assertEqual(out, ["abteh "])

    def test_editing_a_recalled_prompt_into_its_previous_word(self) -> None:
        out = self.assert_safe([b"\x1b[A", b"\x7f\x7f\x7f", b"adn "], recall=["fix it"])
        self.assertEqual(out, ["fixadn "])


class PasteTests(ScenarioCase):
    def test_pasted_text_is_never_corrected(self) -> None:
        out = self.assert_safe_however_chunked(PASTE(b"teh adn recieve"))
        self.assertEqual(out, ["teh adn recieve"])

    def test_correction_resumes_after_a_paste_that_ends_in_a_space(self) -> None:
        out = self.assert_safe_however_chunked(b"see " + PASTE(b"notes.md ") + b"teh ")
        self.assertEqual(out, ["see notes.md the "])

    def test_correction_resumes_after_a_paste_then_a_space(self) -> None:
        out = self.assert_safe_however_chunked(b"see " + PASTE(b"notes.md") + b" teh ")
        self.assertEqual(out, ["see notes.md the "])

    def test_marker_split_at_every_point(self) -> None:
        script = b"fix teh " + PASTE(b"adn teh ") + b"teh " + PASTE(b"x") + b" adn "
        out = self.assert_safe_however_chunked(script)
        self.assertEqual(out, ["fix the adn teh the x and "])

    def test_paste_containing_escape_sequences_is_passed_through(self) -> None:
        out = self.assert_safe_however_chunked(b"a " + PASTE(b"x\x1b[Ay\x1b[<64;1;1M") + b" teh ")
        self.assertIn("the ", out[-1])

    def test_paste_submitted_in_the_same_read(self) -> None:
        out = self.assert_safe_however_chunked(b"fix teh " + PASTE(b"adn") + b"\rteh ")
        self.assertEqual(out, ["fix the adn", "the "])

    def test_two_pastes_back_to_back(self) -> None:
        out = self.assert_safe_however_chunked(PASTE(b"a ") + PASTE(b"b ") + b"teh ")
        self.assertEqual(out, ["a b the "])

    def test_paste_in_the_middle_of_a_word_is_left_alone(self) -> None:
        out = self.assert_safe_however_chunked(b"te" + PASTE(b"XX") + b"h adn ")
        self.assertEqual(out, ["teXXh and "])

    def test_typing_glued_to_the_end_of_a_paste(self) -> None:
        out = self.assert_safe([PASTE(b"path") + b"teh "])
        self.assertEqual(out, ["pathteh "])

    def test_typing_glued_to_a_paste_after_a_backspace(self) -> None:
        out = self.assert_safe([PASTE(b"pasted "), b"\x7f", b"teh. "])
        self.assertEqual(out, ["pastedteh. "])


class TabCompletionTests(ScenarioCase):
    def test_typing_after_a_path_completion_is_not_judged_alone(self) -> None:
        out = self.assert_safe(
            [b"open src/si\t", b"teh "], completions={"src/si": "src/sidequest/"}
        )
        self.assertEqual(out, ["open src/sidequest/teh "])

    def test_words_after_the_completed_word_are_corrected(self) -> None:
        out = self.assert_safe(
            [b"open src/si\t", b"x adn "], completions={"src/si": "src/sidequest/"}
        )
        self.assertEqual(out, ["open src/sidequest/x and "])

    def test_repeated_and_leading_tabs(self) -> None:
        self.assert_recovers(b"open s\t\t\t", completions={"s": "src/"})
        self.assert_recovers(b"\t\t", completions={"s": "src/"})

    def test_tab_between_words_only_affects_the_word_it_touches(self) -> None:
        self.assert_recovers(b"fix teh\tx")

    def test_tab_completion_then_enter_and_next_prompt(self) -> None:
        out = self.assert_safe_however_chunked(
            b"open src/si\t\radn ", completions={"src/si": "src/sidequest/"}
        )
        self.assertEqual(out, ["open src/sidequest/", "and "])

    def test_first_word_after_a_space_terminated_completion_is_left_alone(self) -> None:
        # Deliberately conservative: we can't see whether a completion ended
        # with its own space, so the first word typed after Tab is skipped.
        out = self.assert_safe([b"/mo\t", b"adn teh "], completions={"/mo": "/model "})
        self.assertEqual(out, ["/model adn the "])

    def test_shift_tab_is_transparent(self) -> None:
        for shift_tab in (b"\x1b[Z", b"\x1b[1;2Z", b"\x1b[9;2u"):
            with self.subTest(sequence=shift_tab):
                self.assert_recovers(b"fix teh" + shift_tab)


class SlashAndMentionTests(ScenarioCase):
    def test_slash_command_names_are_never_rewritten(self) -> None:
        for command in (b"/hepl", b"/comapct", b"/mdoel", b"/teh", b"/resume"):
            with self.subTest(command=command):
                out = self.assert_safe_however_chunked(command + b" adn\r")
                self.assertEqual(out, [command.decode() + " and", ""])

    def test_at_mentions_and_paths_are_never_rewritten(self) -> None:
        out = self.assert_safe_however_chunked(b"look at @teh and src/teh and ./teh ~/teh \r")
        self.assertEqual(out, ["look at @teh and src/teh and ./teh ~/teh ", ""])

    def test_words_after_a_slash_command_are_corrected(self) -> None:
        out = self.assert_safe_however_chunked(b"/compact focus on teh adn\r")
        self.assertEqual(out, ["/compact focus on the and", ""])

    def test_slash_menu_completion_then_arguments(self) -> None:
        out = self.assert_safe([b"/mo\t", b"opus teh\r"], completions={"/mo": "/model "})
        self.assertEqual(out, ["/model opus the", ""])

    def test_escape_closes_the_slash_menu_and_typing_carries_on(self) -> None:
        # "/", menu opens, Esc closes it, keep writing a normal prompt.
        self.assert_recovers(b"/", splits=False)


class QueueAndHistoryTests(ScenarioCase):
    """Up / Alt+Up pull earlier or queued messages back into the prompt box."""

    def test_recalled_text_followed_by_a_space_then_new_words(self) -> None:
        out = self.assert_safe([b"\x1b[A adn "], recall=["fix it"])
        self.assertEqual(out, ["fix it and "])

    def test_editing_a_recalled_message(self) -> None:
        # Backspace after a recall could be eating into text we never saw, so
        # the first word typed afterwards is left alone; later words are fine.
        out = self.assert_safe([b"\x1b[A", b"\x7f\x7f", b"adn teh "], recall=["fix it"])
        self.assertEqual(out, ["fix adn the "])

    def test_legacy_alt_up_pops_the_queue(self) -> None:
        out = self.assert_safe([b"\x1b\x1b[A adn "], recall=["fix it"])
        self.assertEqual(out, ["fix it and "])

    def test_kitty_alt_up_is_deliberately_conservative(self) -> None:
        self.assert_suspended(b"\x1b[1;3A", recall=["fix it"])

    def test_recalling_then_submitting_then_typing_again(self) -> None:
        out = self.assert_safe_however_chunked(b"\x1b[A\r adn teh ", recall=["fix teh"])
        self.assertEqual(out, ["fix teh", " and the "])

    def test_popping_two_queued_messages_in_a_row(self) -> None:
        out = self.assert_safe([b"\x1b[A", b"\x1b[A adn "], recall=["one", "two"])
        self.assertEqual(out, ["two and "])

    @known_bug(GLUE)
    def test_typing_glued_to_recalled_text(self) -> None:
        out = self.assert_safe([b"\x1b[Aadn "], recall=["fix it"])
        self.assertEqual(out, ["fix itadn "])

    @known_bug(GLUE)
    def test_typing_glued_to_recalled_text_after_a_backspace(self) -> None:
        out = self.assert_safe([b"\x1b[Ateh ok,\x7f\x7f teh"], recall=["fix it"])
        self.assertNotIn("itthe", "\n".join(out))


class TypeAheadTests(ScenarioCase):
    """Several prompts landing in a single read (paste without brackets, macros,
    tmux send-keys, a busy machine coalescing keystrokes...)."""

    def test_multiple_prompts_in_one_read_each_submit_and_are_corrected(self) -> None:
        out = self.assert_safe_however_chunked(b"fix teh\radn go\rteh\r")
        self.assertEqual(out, ["fix the", "and go", "the", ""])

    def test_lf_and_crlf_line_endings(self) -> None:
        for ending in (b"\r\n", b"\n\r"):
            with self.subTest(ending=ending):
                self.assert_safe_however_chunked(b"fix teh" + ending + b"adn" + ending)

    def test_ctrl_c_between_prompts_clears_the_first(self) -> None:
        out = self.assert_safe([b"teh\x03adn\r"])
        self.assertEqual(out, ["and", ""])

    def test_typing_ahead_while_the_agent_is_busy_then_editing(self) -> None:
        script = b"first teh\r" + b"second adn" + b"\x1b[D" * 3 + b"\x7f" + b"\rthird teh \r"
        self.assert_safe_however_chunked(script)

    @staticmethod
    def _proxy_writes(processor: InputProcessor, data: bytes) -> list[bytes]:
        """What run_in_pty writes to the child for one read of user input."""
        out = processor.feed(data)
        writes, start = [], 0
        for split in processor.pending_submit_splits:
            writes.append(out[start:split])
            start = split
        writes.append(out[start:])
        return writes

    def test_single_correction_before_enter_gets_its_own_write(self) -> None:
        writes = self._proxy_writes(InputProcessor(CORRECTOR), b"fix teh\r")
        self.assertEqual(writes, [b"fix teh\x7f\x7f\x7fthe", b"\r"])

    def test_every_enter_after_a_correction_gets_its_own_write(self) -> None:
        # Otherwise a TUI sees "rewrite + Enter" as one burst and may insert a
        # newline instead of submitting (see pending_submit_split).
        writes = self._proxy_writes(InputProcessor(CORRECTOR), b"teh\radn\rteh\r")
        for write in writes:
            self.assertIsNone(
                re.search(rb"\x7f+[A-Za-z]+[\r\n\x03\x04]", write),
                f"a rewrite and its Enter share one write: {writes!r}",
            )


class ChunkingInvarianceTests(ScenarioCase):
    """Terminals split reads anywhere. The result must not depend on where."""

    SCRIPTS = (
        b"fix teh \x1b[I\x1b[<35;1;1M adn \x1b[?1;2c teh\r",
        b"a\x1b]11;rgb:00/00/00\x07b teh \x1b[12;40R adn ",
        b"/mo\tx adn \x1b[Z teh \x1b[99;5u\x1b[99;5:3u adn ",
        b"one " + PASTE(b"two three ") + b"adn \x1b[200~x\x1b[201~ teh\r",
        b"fix \x1b[A adn \x1b[B teh \x1b[C\x1b[D adn\r",
        b"caf\xc3\xa9 adn \xf0\x9f\x98\x80 teh\r",
    )

    def test_result_is_the_same_however_the_read_is_split(self) -> None:
        for script in self.SCRIPTS:
            with self.subTest(script=script):
                self.assert_safe_however_chunked(
                    script, completions=COMPLETIONS, recall=list(RECALL)
                )

    def test_every_single_split_point(self) -> None:
        for script in self.SCRIPTS:
            whole = self.assert_safe([script], completions=COMPLETIONS, recall=list(RECALL))
            for cut in range(1, len(script)):
                with self.subTest(script=script, cut=cut):
                    split = self.assert_safe(
                        [script[:cut], script[cut:]],
                        completions=COMPLETIONS,
                        recall=list(RECALL),
                    )
                    self.assertEqual(split, whole)


class FuzzTests(ScenarioCase):
    """Random editing sessions built only from keys with settled behaviour."""

    POOL = (
        [
            (word, "word")
            for word in (
                b"teh",
                b"adn",
                b"fix",
                b"the",
                b"x",
                b"recieve",
                b"src/teh",
                b"ok,",
                b"teh.",
                b"ab",
            )
        ]
        * 3
        + [(b" ", "space")] * 6
        + [(b"\r", "enter"), (b"\t", "tab"), (b"\x03", "ctrl-c"), (b"\x17", "ctrl-w")]
        + [(PASTE(b"pasted "), "paste")]
        + [(key, name) for name, key in TRANSPARENT.items()]
        + [(key, name) for name, key in NEWLINE_KEYS_HANDLED.items()]
    )

    def test_random_sessions_never_corrupt_the_prompt(self) -> None:
        rng = random.Random(20260921)
        for iteration in range(1500):
            script = b"".join(rng.choice(self.POOL)[0] for _ in range(rng.randint(2, 10)))
            with self.subTest(iteration=iteration, script=script):
                self.assert_safe_however_chunked(
                    script, completions=COMPLETIONS, recall=list(RECALL)
                )


# --------------------------------------------------------------------------
# Codex: when does Enter mean "a prompt was sent"?
# --------------------------------------------------------------------------


class CodexPanelTriggerTests(unittest.TestCase):
    """AgentLifecycle opens the breaks panel when Enter submits typed text."""

    @staticmethod
    def shows(*chunks: bytes, ready: bool = True) -> int:
        companion = Mock()
        lifecycle = AgentLifecycle(companion, watch_codex_input=True)
        if ready:
            lifecycle.child_output(b"Ask Codex to do anything")
        for chunk in chunks:
            lifecycle.user_input(chunk)
        return companion.show.call_count

    def test_a_typed_prompt_opens_the_panel_once(self) -> None:
        self.assertEqual(self.shows(b"fix it\r"), 1)
        self.assertEqual(self.shows(b"fix it\n"), 1)

    def test_enter_alone_and_slash_commands_do_not(self) -> None:
        for chunks in ([b"\r"], [b"/status\r"], [b"/model opus\r"], [b"ab\x7f\x7f\r"]):
            with self.subTest(chunks=chunks):
                self.assertEqual(self.shows(*chunks), 0)

    def test_nothing_before_the_composer_first_appears(self) -> None:
        self.assertEqual(self.shows(b"y\r", ready=False), 0)

    def test_terminal_noise_before_enter_is_not_a_prompt(self) -> None:
        for noise in (b"\x1b[I\x1b[O", b"\x1b[<64;1;1M", b"\x1b[<35;1;1M", b"\x1b[12;40R"):
            with self.subTest(noise=noise):
                self.assertEqual(self.shows(noise + b"\r"), 0)

    def test_navigation_around_typed_text_still_counts_as_a_prompt(self) -> None:
        for key in (b"\x1b[H", b"\x1b[D", b"\x1b[1;5D", b"\x1b[F"):
            with self.subTest(key=key):
                self.assertEqual(self.shows(b"abc" + key + b"\r"), 1)

    def test_shift_enter_newline_is_not_a_submit(self) -> None:
        self.assertEqual(self.shows(b"abc\x1b[13;2u"), 0)
        self.assertEqual(self.shows(b"abc\x1b[13;2u", b"def\r"), 1)

    def test_several_prompts_in_one_read_keep_the_panel_open(self) -> None:
        self.assertEqual(self.shows(b"one\rtwo\r"), 2)  # show() itself is idempotent

    def test_text_typed_after_ctrl_c_still_counts(self) -> None:
        self.assertEqual(self.shows(b"abc\x03", b"def\r"), 1)

    def test_non_ascii_prompt_counts(self) -> None:
        self.assertEqual(self.shows("héllo — 😀\r".encode()), 1)

    def test_bracketed_paste_then_enter_is_one_prompt(self) -> None:
        self.assertEqual(self.shows(PASTE(b"hello"), b"\r"), 1)

    @known_bug("a multi-line bracketed paste is treated as Enter, opening the panel unsubmitted")
    def test_newlines_inside_a_paste_are_not_submits(self) -> None:
        for newline in (b"\n", b"\r", b"\r\n"):
            with self.subTest(newline=newline):
                self.assertEqual(self.shows(PASTE(b"one" + newline + b"two")), 0)
                self.assertEqual(self.shows(PASTE(b"one" + newline + b"two"), b"\r"), 1)

    @known_bug(
        "clearing the box (Ctrl-C/U/W) leaves the typed count behind, so a blank Enter opens"
    )
    def test_clearing_the_box_then_a_blank_enter_is_not_a_prompt(self) -> None:
        for name, clear in {"ctrl+c": b"\x03", "ctrl+u": b"\x15", "ctrl+w": b"\x17"}.items():
            with self.subTest(clear=name):
                self.assertEqual(self.shows(b"abc", clear, b"\r"), 0)

    @known_bug("Up / Alt+Up recall puts text in the box, but Enter on it isn't counted as a prompt")
    def test_resubmitting_recalled_or_queued_text_opens_the_panel(self) -> None:
        for name, recall in {"up": b"\x1b[A", "alt+up": b"\x1b[1;3A", "ctrl+p": b"\x10"}.items():
            with self.subTest(recall=name):
                self.assertEqual(self.shows(recall, b"\r"), 1)

    @known_bug("Alt+Enter (ESC CR) and Ctrl-J insert a newline but are counted as a submit")
    def test_newline_keys_other_than_shift_enter_are_not_submits(self) -> None:
        for name, newline in {"alt+enter": b"\x1b\r", "ctrl+j": b"\n"}.items():
            with self.subTest(newline=name):
                self.assertEqual(self.shows(b"abc" + newline), 0)

    @known_bug("Meta keys and split escape sequences are counted as typed text")
    def test_keys_that_type_nothing_do_not_make_a_blank_enter_a_prompt(self) -> None:
        for name, chunks in {
            "alt+b": [b"\x1bb", b"\r"],
            "arrow split across reads": [b"\x1b[", b"A", b"\r"],
        }.items():
            with self.subTest(keys=name):
                self.assertEqual(self.shows(*chunks), 0)


if __name__ == "__main__":
    unittest.main()
