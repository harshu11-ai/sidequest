import unittest

from sidequest.autocorrect.corrector import ConservativeCorrector
from sidequest.autocorrect.input_processor import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    InputProcessor,
)


class InputProcessorTests(unittest.TestCase):
    def test_forwards_normal_typing_unchanged(self) -> None:
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"please "), b"please ")

    def test_rewrites_typo_at_space(self) -> None:
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"teh"), b"teh")
        self.assertEqual(processor.feed(b" "), b"\x7f\x7f\x7fthe ")

    def test_rewrites_when_input_arrives_in_one_chunk(self) -> None:
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"fix teh function"), b"fix teh\x7f\x7f\x7fthe function")

    def test_rewrites_liek_at_space(self) -> None:
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"something liek "), b"something liek\x7f\x7f\x7f\x7flike ")

    def test_preserves_punctuation_during_rewrite(self) -> None:
        processor = InputProcessor()
        self.assertEqual(
            processor.feed(b"fix teh, please"),
            b"fix teh,\x7f\x7f\x7f\x7fthe, please",
        )

    def test_immediate_backspace_undoes_correction(self) -> None:
        processor = InputProcessor()
        processor.feed(b"teh ")
        self.assertEqual(processor.feed(b"\x7f"), b"\x7f\x7f\x7f\x7fteh")
        self.assertEqual(processor.feed(b" "), b"\x7f\x7f\x7fthe ")

    def test_expands_abbreviation_and_backspace_restores_trigger(self) -> None:
        corrector = ConservativeCorrector(abbreviations={"pr": "pull request"})
        processor = InputProcessor(corrector)
        self.assertEqual(
            processor.feed(b"pr "),
            b"pr\x7f\x7fpull request ",
        )
        self.assertEqual(
            processor.feed(b"\x7f"),
            b"\x7f" + (b"\x7f" * len(b"pull request")) + b"pr",
        )

    def test_expansion_is_not_processed_recursively(self) -> None:
        corrector = ConservativeCorrector(abbreviations={"a": "b", "b": "expanded"})
        processor = InputProcessor(corrector)
        self.assertEqual(processor.feed(b"a "), b"a\x7fb ")

    def test_expands_abbreviation_before_enter(self) -> None:
        corrector = ConservativeCorrector(abbreviations={"rt": "run tests"})
        processor = InputProcessor(corrector)
        self.assertEqual(processor.feed(b"rt\n"), b"rt\x7f\x7frun tests\n")

    def test_correction_before_enter_marks_a_submit_split(self) -> None:
        # A correction landing right on Enter should be flagged for the PTY
        # loop to send as two writes -- see pending_submit_split's docstring
        # for why a single burst can be misread as a paste by the child.
        processor = InputProcessor()
        result = processor.feed(b"teh\n")
        self.assertEqual(result, b"teh\x7f\x7f\x7fthe\n")
        self.assertEqual(processor.pending_submit_split, len(result) - 1)
        self.assertEqual(result[processor.pending_submit_split :], b"\n")

    def test_correction_before_space_does_not_mark_a_submit_split(self) -> None:
        processor = InputProcessor()
        processor.feed(b"teh ")
        self.assertIsNone(processor.pending_submit_split)

    def test_plain_enter_without_a_correction_does_not_mark_a_submit_split(self) -> None:
        processor = InputProcessor()
        processor.feed(b"hello")
        processor.feed(b"\n")
        self.assertIsNone(processor.pending_submit_split)

    def test_submit_split_does_not_leak_into_the_next_feed_call(self) -> None:
        processor = InputProcessor()
        processor.feed(b"teh\n")
        self.assertIsNotNone(processor.pending_submit_split)
        processor.feed(b"hi")
        self.assertIsNone(processor.pending_submit_split)

    def test_does_not_expand_abbreviation_inside_paste(self) -> None:
        corrector = ConservativeCorrector(abbreviations={"pr": "pull request"})
        processor = InputProcessor(corrector)
        pasted = BRACKETED_PASTE_START + b"pr " + BRACKETED_PASTE_END
        self.assertEqual(processor.feed(pasted), pasted)

    def test_editing_word_with_backspace_updates_buffer(self) -> None:
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"tehh\x7f "), b"tehh\x7f\x7f\x7f\x7fthe ")

    def test_ctrl_h_word_erase_resets_buffer(self) -> None:
        processor = InputProcessor()
        self.assertEqual(
            processor.feed(b"teh\x08teh "),
            b"teh\x08teh\x7f\x7f\x7fthe ",
        )

    def test_ctrl_w_word_erase_resets_buffer(self) -> None:
        processor = InputProcessor()
        self.assertEqual(
            processor.feed(b"teh\x17teh "),
            b"teh\x17teh\x7f\x7f\x7fthe ",
        )

    def test_ctrl_backspace_does_not_trigger_autocorrect_undo(self) -> None:
        processor = InputProcessor()
        processor.feed(b"teh ")
        self.assertEqual(processor.feed(b"\x08"), b"\x08")
        self.assertEqual(processor.feed(b"teh "), b"teh\x7f\x7f\x7fthe ")

    def test_path_is_passed_through(self) -> None:
        processor = InputProcessor()
        value = b"src/teh.py "
        self.assertEqual(processor.feed(value), value)

    def test_arrow_key_with_nothing_in_flight_does_not_suspend_correction(self) -> None:
        # A correction only ever backspaces exactly what it tracked for the
        # word it's rewriting -- with no word in flight when the arrow key
        # arrives, there's nothing for the cursor jump to put at risk, so a
        # fresh word typed right after it should still get corrected.
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"\x1b[Ateh "), b"\x1b[Ateh\x7f\x7f\x7fthe ")

    def test_arrow_key_mid_word_suspends_correction_until_new_line(self) -> None:
        # Interrupting a word that's actively being composed is the case
        # that's actually unsafe: what gets typed next may no longer line up
        # with where the tracker thinks the cursor is.
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"te"), b"te")
        self.assertEqual(processor.feed(b"\x1b[A"), b"\x1b[A")
        self.assertEqual(processor.feed(b"h "), b"h ")
        self.assertEqual(processor.feed(b"\nteh "), b"\nteh\x7f\x7f\x7fthe ")

    def test_ss3_arrow_key_with_nothing_in_flight_does_not_suspend_correction(self) -> None:
        # Some terminals send arrows as SS3 (application cursor mode)
        # rather than CSI; both encodings get the same gentler treatment.
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"\x1bOAteh "), b"\x1bOAteh\x7f\x7f\x7fthe ")

    def test_non_arrow_csi_key_still_suspends_correction_with_nothing_in_flight(self) -> None:
        # Confirms the relaxation is scoped to plain navigation, not
        # unrecognized escape sequences generally: an unknown key (here,
        # the legacy Delete key) still suspends correction for the rest of
        # the line even with nothing in flight when it arrives.
        processor = InputProcessor()
        delete_key = b"\x1b[3~"
        self.assertEqual(processor.feed(delete_key + b"teh "), delete_key + b"teh ")
        self.assertEqual(processor.feed(b"\nteh "), b"\nteh\x7f\x7f\x7fthe ")

    def test_terminal_startup_reports_do_not_suspend_correction(self) -> None:
        processor = InputProcessor()
        reports = (
            b"\x1b[24;80R"
            b"\x1b[?1;2c"
            b"\x1b[?0u"
            b"\x1b[I"
            b"\x1b]10;rgb:ffff/ffff/ffff\x07"
        )
        self.assertEqual(
            processor.feed(reports + b"teh "),
            reports + b"teh\x7f\x7f\x7fthe ",
        )

    def test_bracketed_paste_is_never_corrected(self) -> None:
        processor = InputProcessor()
        pasted = BRACKETED_PASTE_START + b"teh fucntion" + BRACKETED_PASTE_END
        self.assertEqual(processor.feed(pasted), pasted)
        self.assertEqual(processor.feed(b" more teh "), b" more teh ")
        self.assertEqual(processor.feed(b"\nteh "), b"\nteh\x7f\x7f\x7fthe ")

    def test_ctrl_c_resets_safe_typing_state(self) -> None:
        processor = InputProcessor()
        # Interrupt a word actually in flight, not a blank line, so this is
        # exercising Ctrl-C's reset rather than depending on whether an
        # empty-buffer arrow key itself poisons state (it no longer does).
        processor.feed(b"x\x1b[A")
        self.assertEqual(processor.feed(b"teh \x03teh "), b"teh \x03teh\x7f\x7f\x7fthe ")

    def test_enhanced_ctrl_c_resets_safe_typing_state(self) -> None:
        processor = InputProcessor()
        processor.feed(b"x\x1b[A")
        ctrl_c = b"\x1b[99;5u"
        self.assertEqual(
            processor.feed(b"teh " + ctrl_c + b"teh "),
            b"teh " + ctrl_c + b"teh\x7f\x7f\x7fthe ",
        )

    def test_enhanced_ctrl_c_release_does_not_disable_correction(self) -> None:
        processor = InputProcessor()
        ctrl_c_press_and_release = b"\x1b[99;5u\x1b[99;5:3u"
        self.assertEqual(
            processor.feed(ctrl_c_press_and_release + b"teh "),
            ctrl_c_press_and_release + b"teh\x7f\x7f\x7fthe ",
        )

    def test_enhanced_ctrl_d_resets_safe_typing_state(self) -> None:
        processor = InputProcessor()
        processor.feed(b"\x1b[A")
        ctrl_d = b"\x1b[100;5u"
        self.assertEqual(
            processor.feed(ctrl_d + b"teh "),
            ctrl_d + b"teh\x7f\x7f\x7fthe ",
        )

    def test_enhanced_escape_resets_safe_typing_state(self) -> None:
        processor = InputProcessor()
        processor.feed(b"\x1b[A")
        escape = b"\x1b[27u"
        self.assertEqual(
            processor.feed(escape + b"teh "),
            escape + b"teh\x7f\x7f\x7fthe ",
        )

    def test_legacy_double_escape_resets_typing_state(self) -> None:
        processor = InputProcessor()
        double_escape = b"\x1b\x1b"
        self.assertEqual(
            processor.feed(b"teh" + double_escape + b"teh "),
            b"teh" + double_escape + b"teh\x7f\x7f\x7fthe ",
        )

    def test_legacy_double_escape_split_across_reads_resets_typing_state(self) -> None:
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"teh\x1b"), b"teh\x1b")
        self.assertEqual(
            processor.feed(b"\x1bteh "),
            b"\x1bteh\x7f\x7f\x7fthe ",
        )

    def test_enhanced_double_escape_resets_typing_state(self) -> None:
        processor = InputProcessor()
        double_escape = b"\x1b[27u\x1b[27;1:3u\x1b[27u"
        self.assertEqual(
            processor.feed(b"teh" + double_escape + b"teh "),
            b"teh" + double_escape + b"teh\x7f\x7f\x7fthe ",
        )

    def test_alt_key_sequence_remains_conservative(self) -> None:
        processor = InputProcessor()
        alt_x = b"\x1bx"
        self.assertEqual(processor.feed(b"teh" + alt_x + b"teh "), b"teh" + alt_x + b"teh ")
        self.assertEqual(processor.feed(b"\nteh "), b"\nteh\x7f\x7f\x7fthe ")

    def test_legacy_shift_tab_preserves_typing_state(self) -> None:
        processor = InputProcessor()
        shift_tab = b"\x1b[Z"
        self.assertEqual(
            processor.feed(b"teh" + shift_tab + b" "),
            b"teh" + shift_tab + b"\x7f\x7f\x7fthe ",
        )

    def test_enhanced_shift_tab_events_preserve_typing_state(self) -> None:
        processor = InputProcessor()
        shift_tab_press_and_release = b"\x1b[9;2u\x1b[9;2:3u"
        self.assertEqual(
            processor.feed(b"teh" + shift_tab_press_and_release + b" "),
            b"teh" + shift_tab_press_and_release + b"\x7f\x7f\x7fthe ",
        )

    def test_claude_mouse_motion_preserves_typing_state(self) -> None:
        processor = InputProcessor()
        mouse_motion = b"\x1b[<35;80;24M"
        self.assertEqual(
            processor.feed(b"teh" + mouse_motion + b" "),
            b"teh" + mouse_motion + b"\x7f\x7f\x7fthe ",
        )

    def test_claude_mouse_motion_split_across_reads_preserves_typing_state(self) -> None:
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"teh\x1b[<35;"), b"teh\x1b[<35;")
        self.assertEqual(processor.feed(b"80;24M "), b"80;24M\x7f\x7f\x7fthe ")

    def test_claude_mouse_click_still_suspends_correction(self) -> None:
        processor = InputProcessor()
        mouse_click = b"\x1b[<0;80;24M"
        self.assertEqual(
            processor.feed(b"teh" + mouse_click + b"teh "),
            b"teh" + mouse_click + b"teh ",
        )
        self.assertEqual(processor.feed(b"\nteh "), b"\nteh\x7f\x7f\x7fthe ")

    def test_enhanced_word_erase_resets_buffer(self) -> None:
        sequences = (
            b"\x1b[127;5u",  # Kitty Ctrl-Backspace
            b"\x1b[8;5u",  # alternate Kitty Ctrl-Backspace
            b"\x1b[127;3u",  # Kitty Option-Backspace
            b"\x1b[119;5u",  # Kitty Ctrl-W
            b"\x1b[27;5;127~",  # xterm Ctrl-Backspace
            b"\x1b[27;3;127~",  # xterm Option-Backspace
        )
        for sequence in sequences:
            with self.subTest(sequence=sequence):
                processor = InputProcessor()
                self.assertEqual(
                    processor.feed(b"teh" + sequence + b"teh "),
                    b"teh" + sequence + b"teh\x7f\x7f\x7fthe ",
                )

    def test_legacy_option_backspace_resets_buffer(self) -> None:
        processor = InputProcessor()
        word_erase = b"\x1b\x7f"
        self.assertEqual(
            processor.feed(b"teh" + word_erase + b"teh "),
            b"teh" + word_erase + b"teh\x7f\x7f\x7fthe ",
        )

    def test_unknown_enhanced_editing_key_still_suspends_correction(self) -> None:
        processor = InputProcessor()
        ctrl_k = b"\x1b[107;5u"
        self.assertEqual(processor.feed(b"teh" + ctrl_k + b"teh "), b"teh" + ctrl_k + b"teh ")
        self.assertEqual(processor.feed(b"\nteh "), b"\nteh\x7f\x7f\x7fthe ")

    def test_tab_is_passed_through_and_suspends_correction(self) -> None:
        processor = InputProcessor()
        self.assertEqual(processor.feed(b"teh\tteh "), b"teh\tteh ")
        self.assertEqual(processor.feed(b"\nteh "), b"\nteh\x7f\x7f\x7fthe ")


if __name__ == "__main__":
    unittest.main()
