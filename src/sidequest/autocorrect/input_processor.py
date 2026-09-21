"""Stateful processing of bytes read from the user's terminal."""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.autocorrect.corrector import Correction, CorrectionEngine, get_default_corrector

ESCAPE = 0x1B
BACKSPACE = 0x7F
WORD_ERASE_BYTES = {0x08, 0x17}  # Ctrl-H/Ctrl-Backspace, Ctrl-W
RESET_BYTES = {0x03, 0x04, 0x0A, 0x0D}  # Ctrl-C, Ctrl-D, LF, CR
BOUNDARY_BYTES = {0x0A, 0x0D, 0x20}  # LF, CR, space
TAB = 0x09
BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"
KITTY_SHIFT = 1
KITTY_ALT = 2
KITTY_CTRL = 4
_ARROW_FINAL_BYTES = {ord("A"), ord("B"), ord("C"), ord("D")}  # up, down, right, left
_HOME_END_FINAL_BYTES = {ord("H"), ord("F")}
# Home, End, Page Up/Down in their CSI n ~ forms. (Delete is deliberately
# absent: it edits text, so it keeps the conservative treatment.)
_NAVIGATION_TILDE_KEYS = {b"1", b"4", b"5", b"6", b"7", b"8"}
# DCS, APC, PM and SOS strings: terminal replies that end with ST (ESC \).
_STRING_INTRODUCERS = (b"\x1bP", b"\x1b_", b"\x1b^", b"\x1bX")
_MAX_STRING_REPLY = 1024
# Bytes that can follow a lone ESC and still be part of one escape sequence.
_SEQUENCE_CONTINUATIONS = b"[O]P_^X"
_PASTE_WHITESPACE = {0x09, 0x0A, 0x0D, 0x20}
_CURSOR_MOTION_CONTROLS = {0x01, 0x05}  # Ctrl-A, Ctrl-E: line start / end


@dataclass(slots=True)
class AppliedCorrection:
    original: bytes
    replacement: bytes
    boundary: int


class InputProcessor:
    """Track linear typing and emit safe rewrite sequences at word boundaries."""

    def __init__(self, corrector: CorrectionEngine | None = None) -> None:
        self.corrector = corrector or get_default_corrector()
        self.token = bytearray()
        self.safe_to_correct = True
        self.in_paste = False
        # Set when the word being typed can't be judged on its own (Tab
        # completion, non-ASCII, backspacing into an earlier word, a paste
        # that ended mid-word, ...): it is left alone, and the next typed
        # space ends it and correction resumes. See _taint_word.
        self._resume_after_word = False
        # Characters typed on this line so far, or None once text we never saw
        # (a recall, a paste, a completion) may be present. It only decides
        # whether a Backspace with nothing tracked could be eating into text.
        self._line_length: int | None = 0
        self._paste_last: int | None = None
        self._escape_candidate = bytearray()
        self._paste_end_candidate = bytearray()
        self.last_correction: AppliedCorrection | None = None
        # Indexes into the bytes just returned by feed() where a submit
        # boundary (Enter, Ctrl-C, Ctrl-D, ...) landed right after a
        # correction was rewritten in. Empty when the last feed() call had
        # nothing to split. See _handle_boundary for why this matters.
        self.pending_submit_splits: list[int] = []

    @property
    def pending_submit_split(self) -> int | None:
        """The last split index, for callers that only care about one."""
        return self.pending_submit_splits[-1] if self.pending_submit_splits else None

    def feed(self, data: bytes) -> bytes:
        """Process input bytes and return bytes to send to the child PTY."""
        output = bytearray()
        self.pending_submit_splits = []
        self._resolve_lone_escape(data)
        for byte in data:
            if self.in_paste:
                output.append(byte)
                self._track_paste_end(byte)
                continue

            if self._escape_candidate:
                output.append(byte)
                self._track_escape(byte)
                continue

            if byte == ESCAPE:
                self._escape_candidate.append(byte)
                output.append(byte)
                continue

            if byte in WORD_ERASE_BYTES:
                self._reset_line()
                output.append(byte)
                continue

            if self._undo_if_requested(byte, output):
                continue

            if self.last_correction is not None:
                self.last_correction = None

            if byte == BACKSPACE:
                if self.safe_to_correct:
                    if self.token:
                        self.token.pop()
                    elif self._line_length != 0:
                        # Nothing we typed is left to delete, so this eats
                        # into an earlier word: whatever is typed next isn't
                        # a whole word on its own.
                        self._taint_word()
                self._note_deleted()
                output.append(byte)
                continue

            if byte in BOUNDARY_BYTES:
                self._handle_boundary(byte, output)
                continue

            if byte == TAB:
                output.append(byte)
                self._taint_word()
                self._line_length = None
                continue

            if byte < 0x20:
                output.append(byte)
                if byte in RESET_BYTES:
                    self._reset_line(empty=byte == 0x03)
                elif byte in _CURSOR_MOTION_CONTROLS:
                    self._invalidate_in_flight_word()
                else:
                    self._invalidate_line()
                continue

            output.append(byte)
            self._note_typed()
            if self.safe_to_correct:
                if byte < 0x80:
                    self.token.append(byte)
                else:
                    # BS removes a whole character, not a byte, so a multi-byte
                    # word can't be tracked -- but it only spoils this word.
                    self._taint_word()

        return bytes(output)

    def _resolve_lone_escape(self, data: bytes) -> None:
        """Decide whether an ESC that ended the previous read was the Esc key.

        A terminal delivers a whole escape sequence in one write, so an ESC
        left dangling at the end of a read followed later by ordinary input is
        the Esc key on its own (interrupting the agent, closing a menu). It
        edits nothing, so it must not swallow the next typed character.
        """
        if self._escape_candidate != b"\x1b" or not data or self.in_paste:
            return
        first = data[0]
        if first == ESCAPE:
            # ESC ESC is the legacy double-Esc, unless the second ESC opens a
            # real sequence (Esc, then an arrow key in the next read).
            continues = data[1:2] not in (b"[", b"O", b"]")
        else:
            continues = first in _SEQUENCE_CONTINUATIONS
        if not continues:
            self._escape_candidate.clear()
            # Esc edits nothing, but if a rewrite (backspaces) followed it
            # straight away a TUI still inside its Esc timeout could read that
            # as Alt+Backspace. Leaving an in-flight word alone rules that out.
            if self.token:
                self._taint_word()

    def _note_typed(self) -> None:
        if self._line_length is not None:
            self._line_length += 1

    def _note_deleted(self) -> None:
        if self._line_length:
            self._line_length -= 1

    def _taint_word(self) -> None:
        """Leave the word being typed alone, but keep the rest of the line live.

        Some things put text before the cursor that the tracker never saw --
        Tab completion, a non-ASCII character, a Backspace into an earlier
        word, a paste that ended mid-word -- so the typed tail can't be judged
        or safely backspaced as a whole word. Once a typed space ends that
        word the cursor is back at a known position, so correction can resume
        there (the same reasoning as resuming after a bracketed paste).
        """
        if self.safe_to_correct:
            self._resume_after_word = True
        self.safe_to_correct = False
        self.token.clear()
        self.last_correction = None

    def _handle_boundary(self, boundary: int, output: bytearray) -> None:
        if boundary in RESET_BYTES:
            # Enter starts a fresh line, so a pending "resume at the next
            # space" from an earlier Tab must not swallow that line's first word.
            self._resume_after_word = False
            # CR submits an empty box; LF is a newline inside a multi-line
            # prompt, where Backspace can join the lines back up.
            self._line_length = 0 if boundary == 0x0D else None
        elif boundary == 0x20:
            self._note_typed()
        if self._resume_after_word and boundary == 0x20:
            self._resume_after_word = False
            self.safe_to_correct = True
            output.append(boundary)
            self.token.clear()
            return

        if self.safe_to_correct and self.token:
            original = bytes(self.token)
            correction = self._suggest(original)
            if correction is not None:
                replacement = correction.replacement.encode("ascii")
                output.extend(b"\x7f" * len(original))
                output.extend(replacement)
                if boundary in RESET_BYTES:
                    # Without a split, the child receives backspaces +
                    # replacement + Enter/Ctrl-C/Ctrl-D as one uninterrupted
                    # burst with no inter-key delay -- several agent TUIs
                    # mistake that shape for a paste and insert a literal
                    # newline instead of submitting. The PTY loop uses this
                    # index to send the boundary byte as its own write.
                    self.pending_submit_splits.append(len(output))
                if self._line_length is not None and boundary == 0x20:
                    self._line_length += len(replacement) - len(original)
                output.append(boundary)
                if boundary == 0x20:
                    self.last_correction = AppliedCorrection(
                        original=original,
                        replacement=replacement,
                        boundary=boundary,
                    )
                self.token.clear()
                if boundary in RESET_BYTES:
                    self.safe_to_correct = True
                return

        output.append(boundary)
        self.token.clear()
        if boundary in RESET_BYTES:
            self.safe_to_correct = True

    def _suggest(self, original: bytes) -> Correction | None:
        try:
            token = original.decode("ascii")
        except UnicodeDecodeError:
            return None
        return self.corrector.suggest(token)

    def _undo_if_requested(self, byte: int, output: bytearray) -> bool:
        correction = self.last_correction
        if correction is None or byte != BACKSPACE:
            return False

        # Remove the trailing space, remove the replacement, and restore the
        # original. This mirrors the familiar "backspace to undo autocorrect"
        # interaction without claiming Ctrl-Z from the child application.
        output.append(byte)
        output.extend(bytes([byte]) * len(correction.replacement))
        output.extend(correction.original)
        if self._line_length is not None:
            self._line_length += len(correction.original) - len(correction.replacement) - 1
            self._line_length = max(self._line_length, 0)
        self.token[:] = correction.original
        self.last_correction = None
        return True

    def _invalidate_line(self) -> None:
        self.safe_to_correct = False
        self._resume_after_word = False
        self._line_length = None
        self.token.clear()
        self.last_correction = None

    def _invalidate_in_flight_word(self) -> None:
        """Suspend correction only if a word is actually being composed.

        For events we know only ever move the cursor (plain navigation
        keys) rather than insert content the tracker doesn't know about,
        the risk is narrower than an arbitrary unrecognized escape
        sequence: a correction only ever backspaces exactly what feed()
        itself tracked for the in-flight word, so navigation only risks
        corrupting that if it interrupts a word actively being typed right
        now. With nothing in flight, there's nothing for it to corrupt, so
        a fresh word typed right after stays correctable.
        """
        if self.token:
            self.safe_to_correct = False
        self.token.clear()
        self.last_correction = None
        # Up/Down recall and jumps can leave text we never saw around the cursor.
        self._line_length = None

    def _reset_line(self, *, empty: bool = False) -> None:
        """Start tracking afresh; `empty` when the box is known to be empty."""
        self.safe_to_correct = True
        self._resume_after_word = False
        self._line_length = 0 if empty else None
        self.token.clear()
        self.last_correction = None

    def _track_escape(self, byte: int) -> None:
        self._escape_candidate.append(byte)
        candidate = bytes(self._escape_candidate)
        if candidate == b"\x1b\x1b":
            self._reset_line()
            self._escape_candidate.clear()
            return

        if candidate == BRACKETED_PASTE_START:
            self._invalidate_line()
            self.in_paste = True
            self._paste_last = None
            self._escape_candidate.clear()
            self._paste_end_candidate.clear()
            return

        if BRACKETED_PASTE_START.startswith(candidate):
            return

        if candidate.startswith(b"\x1b["):
            if self._csi_is_complete(candidate):
                if self._is_enhanced_line_reset(candidate):
                    self._reset_line()
                elif self._is_navigation_key(candidate):
                    self._invalidate_in_flight_word()
                elif not (
                    self._is_terminal_report(candidate)
                    or self._is_safe_mode_key(candidate)
                    or self._is_passive_mouse_event(candidate)
                ):
                    self._invalidate_line()
                self._escape_candidate.clear()
            elif len(candidate) >= 64:
                self._invalidate_line()
                self._escape_candidate.clear()
            return

        if candidate.startswith(b"\x1bO"):
            if len(candidate) >= 3:
                if self._is_navigation_key(candidate):
                    self._invalidate_in_flight_word()
                else:
                    self._invalidate_line()
                self._escape_candidate.clear()
            return

        if candidate in {b"\x1b\x08", b"\x1b\x7f"}:  # legacy Option/Ctrl-Backspace
            self._reset_line()
            self._escape_candidate.clear()
            return

        if candidate in {b"\x1b\r", b"\x1b\n"}:  # legacy Alt+Enter: a newline in the box
            self._reset_line()
            self._escape_candidate.clear()
            return

        if candidate in {b"\x1bb", b"\x1bf"}:  # legacy Alt+B / Alt+F: word jumps
            self._invalidate_in_flight_word()
            self._escape_candidate.clear()
            return

        # DCS / APC / PM / SOS strings are terminal replies (XTVERSION, kitty
        # graphics status, ...): they end with ST and never edit the prompt.
        if candidate[:2] in _STRING_INTRODUCERS:
            if candidate.endswith(b"\x1b\\"):
                self._escape_candidate.clear()
            elif byte in RESET_BYTES:
                # A real reply never contains Enter or Ctrl-C/D; this was a
                # typed Alt+key, and the line it was on has ended.
                self._reset_line()
                self._escape_candidate.clear()
            elif len(candidate) >= _MAX_STRING_REPLY:
                self._invalidate_line()
                self._escape_candidate.clear()
            return

        # OSC responses include terminal color and title reports. They are
        # terminated by BEL or ST and are not user editing actions.
        if candidate.startswith(b"\x1b]"):
            if byte == 0x07 or candidate.endswith(b"\x1b\\"):
                self._escape_candidate.clear()
            elif len(candidate) >= 256:
                self._invalidate_line()
                self._escape_candidate.clear()
            return

        # ESC followed by any other byte represents an application key or an
        # unknown sequence. Pass it through, but stop correcting this line.
        if len(candidate) >= 2:
            self._invalidate_line()
            self._escape_candidate.clear()

    @staticmethod
    def _csi_is_complete(candidate: bytes) -> bool:
        return len(candidate) >= 3 and 0x40 <= candidate[-1] <= 0x7E

    @classmethod
    def _is_navigation_key(cls, candidate: bytes) -> bool:
        """Recognize keys that only move the cursor and insert nothing.

        Plain arrows, Home/End, Page Up/Down and the modified Left/Right that
        jump by word. These get gentler treatment than an arbitrary
        unrecognized escape sequence (see _invalidate_in_flight_word).
        Modified Up/Down are excluded: Alt+Up pulls a queued message back into
        the prompt, which is an edit.
        """
        if len(candidate) == 3 and candidate[:2] in (b"\x1b[", b"\x1bO"):
            return candidate[2] in _ARROW_FINAL_BYTES or candidate[2] in _HOME_END_FINAL_BYTES
        if not candidate.startswith(b"\x1b[") or len(candidate) < 4:
            return False
        parameters, final = candidate[2:-1], candidate[-1]
        if not cls._numeric_fields(parameters):
            return False
        if final in _HOME_END_FINAL_BYTES or final in (ord("C"), ord("D")):
            return True
        return final == ord("~") and parameters.split(b";")[0] in _NAVIGATION_TILDE_KEYS

    @staticmethod
    def _numeric_fields(parameters: bytes) -> bool:
        fields = parameters.replace(b":", b";").split(b";")
        return all(field.isdigit() or not field for field in fields)

    @staticmethod
    def _is_terminal_report(candidate: bytes) -> bool:
        """Identify terminal-generated replies that are not user keypresses."""
        if candidate in {b"\x1b[I", b"\x1b[O"}:  # focus in/out
            return True

        body = candidate[2:]
        parameters = body[:-1]
        final = body[-1:]

        if final == b"R":  # cursor position report: CSI row ; column R
            parts = parameters.split(b";")
            return len(parts) == 2 and all(part.isdigit() for part in parts)
        if final == b"c":  # primary/secondary device attributes
            return not parameters or parameters[:1] in {b"?", b">", b"="}
        if final in {b"n", b"t"}:  # device/window status reports
            normalized = parameters.lstrip(b"?").replace(b";", b"")
            return bool(normalized) and normalized.isdigit()
        if final == b"u":  # keyboard protocol capability report
            return parameters[:1] in {b"?", b">"}
        if final == b"y":  # DECRPM mode report: CSI [?] mode ; state $ y
            modes = parameters[:-1].lstrip(b"?").replace(b";", b"")
            return parameters.endswith(b"$") and modes.isdigit()
        return False

    @classmethod
    def _is_enhanced_line_reset(cls, candidate: bytes) -> bool:
        """Recognize enhanced keys that abandon, finish, or erase the current input."""
        key = cls._enhanced_key(candidate)
        if key is None:
            return False
        key_code, modifiers = key
        if key_code == ESCAPE:
            return True
        if key_code == 13:  # Enter, Shift+Enter, Alt+Enter, ...: a new line either way
            return True
        if key_code in {0x08, 0x7F}:
            return bool(modifiers & (KITTY_ALT | KITTY_CTRL))
        return key_code in {ord("c"), ord("d"), ord("w")} and bool(modifiers & KITTY_CTRL)

    @classmethod
    def _is_safe_mode_key(cls, candidate: bytes) -> bool:
        """Recognize mode-switching keys that do not edit the prompt text."""
        if candidate in {b"\x1b[Z", b"\x1b[1;2Z"}:  # legacy Shift-Tab
            return True

        key = cls._enhanced_key(candidate)
        if key is None:
            return False
        key_code, modifiers = key
        return key_code == 0x09 and modifiers == KITTY_SHIFT

    @classmethod
    def _enhanced_key(cls, candidate: bytes) -> tuple[int, int] | None:
        return cls._kitty_key(candidate) or cls._modify_other_key(candidate)

    @staticmethod
    def _is_passive_mouse_event(candidate: bytes) -> bool:
        """Recognize SGR mouse reports that cannot edit or move the prompt cursor.

        Motion, wheel ticks and button releases. A button press (a click)
        still counts as unknown, because some apps place the cursor with it.
        """
        if not candidate.startswith(b"\x1b[<") or candidate[-1:] not in {b"M", b"m"}:
            return False

        fields = candidate[3:-1].split(b";")
        if len(fields) != 3 or not all(field.isdigit() for field in fields):
            return False
        if candidate.endswith(b"m"):
            return True
        return bool(int(fields[0]) & 0x60)  # 0x20 motion, 0x40 wheel

    @staticmethod
    def _kitty_key(candidate: bytes) -> tuple[int, int] | None:
        """Parse the key code and modifier bitset from a Kitty CSI-u event."""
        if not candidate.startswith(b"\x1b[") or not candidate.endswith(b"u"):
            return None

        fields = candidate[2:-1].split(b";")
        if not fields or fields[0][:1] in {b"?", b">", b"="}:
            return None

        key_code = fields[0].split(b":", 1)[0]
        modifier = fields[1].split(b":", 1)[0] if len(fields) > 1 else b"1"
        if not key_code.isdigit() or not modifier.isdigit():
            return None

        # Kitty encodes modifiers as one plus a bitset. The optional event type
        # follows a colon, so press, repeat, and release events parse identically.
        return int(key_code), max(int(modifier) - 1, 0)

    @staticmethod
    def _modify_other_key(candidate: bytes) -> tuple[int, int] | None:
        """Parse an xterm modifyOtherKeys CSI 27;modifier;key~ event."""
        if not candidate.startswith(b"\x1b[27;") or not candidate.endswith(b"~"):
            return None
        fields = candidate[2:-1].split(b";")
        if len(fields) != 3 or not all(field.isdigit() for field in fields):
            return None
        return int(fields[2]), max(int(fields[1]) - 1, 0)

    def _track_paste_end(self, byte: int) -> None:
        candidate = self._paste_end_candidate
        candidate.append(byte)
        while candidate and not BRACKETED_PASTE_END.startswith(candidate):
            del candidate[0]
        if not candidate:
            self._paste_last = byte  # a body byte, not part of the end marker
        if bytes(candidate) == BRACKETED_PASTE_END:
            self.in_paste = False
            candidate.clear()
            # A completed paste leaves the cursor right after the inserted
            # text and the token buffer was never fed any pasted bytes, so
            # nothing pasted can ever be backspaced into: correction can
            # resume in the same line. If the paste ended mid-word, though,
            # what's typed next is that word's tail, not a word of its own.
            self._line_length = None
            self.safe_to_correct = True
            if self._paste_last not in _PASTE_WHITESPACE and self._paste_last is not None:
                self._taint_word()
