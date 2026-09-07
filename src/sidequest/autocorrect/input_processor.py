"""Stateful processing of bytes read from the user's terminal."""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.autocorrect.corrector import Correction, CorrectionEngine, get_default_corrector

ESCAPE = 0x1B
BACKSPACE = 0x7F
WORD_ERASE_BYTES = {0x08, 0x17}  # Ctrl-H/Ctrl-Backspace, Ctrl-W
RESET_BYTES = {0x03, 0x04, 0x0A, 0x0D}  # Ctrl-C, Ctrl-D, LF, CR
BOUNDARY_BYTES = {0x0A, 0x0D, 0x20}  # LF, CR, space
BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"
KITTY_SHIFT = 1
KITTY_ALT = 2
KITTY_CTRL = 4


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
        self._escape_candidate = bytearray()
        self._paste_end_candidate = bytearray()
        self.last_correction: AppliedCorrection | None = None

    def feed(self, data: bytes) -> bytes:
        """Process input bytes and return bytes to send to the child PTY."""
        output = bytearray()
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
                if self.safe_to_correct and self.token:
                    self.token.pop()
                output.append(byte)
                continue

            if byte in BOUNDARY_BYTES:
                self._handle_boundary(byte, output)
                continue

            if byte < 0x20:
                output.append(byte)
                if byte in RESET_BYTES:
                    self._reset_line()
                else:
                    self._invalidate_line()
                continue

            output.append(byte)
            if self.safe_to_correct:
                if byte < 0x80:
                    self.token.append(byte)
                else:
                    self._invalidate_line()

        return bytes(output)

    def _handle_boundary(self, boundary: int, output: bytearray) -> None:
        if self.safe_to_correct and self.token:
            original = bytes(self.token)
            correction = self._suggest(original)
            if correction is not None:
                replacement = correction.replacement.encode("ascii")
                output.extend(b"\x7f" * len(original))
                output.extend(replacement)
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
        self.token[:] = correction.original
        self.last_correction = None
        return True

    def _invalidate_line(self) -> None:
        self.safe_to_correct = False
        self.token.clear()
        self.last_correction = None

    def _reset_line(self) -> None:
        self.safe_to_correct = True
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
            self._escape_candidate.clear()
            self._paste_end_candidate.clear()
            return

        if BRACKETED_PASTE_START.startswith(candidate):
            return

        if candidate.startswith(b"\x1b["):
            if self._csi_is_complete(candidate):
                if self._is_enhanced_line_reset(candidate):
                    self._reset_line()
                elif not (
                    self._is_terminal_report(candidate)
                    or self._is_safe_mode_key(candidate)
                    or self._is_mouse_motion(candidate)
                ):
                    self._invalidate_line()
                self._escape_candidate.clear()
            elif len(candidate) >= 64:
                self._invalidate_line()
                self._escape_candidate.clear()
            return

        if candidate.startswith(b"\x1bO"):
            if len(candidate) >= 3:
                self._invalidate_line()
                self._escape_candidate.clear()
            return

        if candidate in {b"\x1b\x08", b"\x1b\x7f"}:  # legacy Option/Ctrl-Backspace
            self._reset_line()
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
    def _is_mouse_motion(candidate: bytes) -> bool:
        """Recognize SGR mouse motion reports, which cannot edit prompt text."""
        if not candidate.startswith(b"\x1b[<") or not candidate.endswith(b"M"):
            return False

        fields = candidate[3:-1].split(b";")
        if len(fields) != 3 or not all(field.isdigit() for field in fields):
            return False

        button_code = int(fields[0])
        return bool(button_code & 0x20)

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
        if bytes(candidate) == BRACKETED_PASTE_END:
            self.in_paste = False
            candidate.clear()
