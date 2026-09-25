import json
import unittest
from unittest.mock import Mock

from sidequest.lifecycle import (
    AgentLifecycle,
    ComposerTracker,
    _contains_codex_prompt,
    _contains_osc9,
    prepare_agent_command,
)


class LifecycleTests(unittest.TestCase):
    def test_codex_enter_is_ignored_until_prompt_is_ready(self) -> None:
        companion = Mock()
        lifecycle = AgentLifecycle(companion, watch_codex_input=True)

        lifecycle.user_input(b"startup confirmation\r")
        companion.show.assert_not_called()

        lifecycle.child_output(b"Ask Codex to do anything")
        lifecycle.user_input(b"fix the bug\r")
        companion.show.assert_called_once_with()

    def test_codex_blank_enter_does_not_open_companion(self) -> None:
        """A bare Enter (dismissing a dialog, an accidental keystroke, ...)
        after the placeholder has ever been drawn must not be mistaken for a
        submitted prompt."""
        companion = Mock()
        lifecycle = AgentLifecycle(companion, watch_codex_input=True)

        lifecycle.child_output(b"Ask Codex to do anything")
        lifecycle.user_input(b"\r")
        companion.show.assert_not_called()

        lifecycle.user_input(b"now a real prompt\r")
        companion.show.assert_called_once_with()

    def test_codex_backspacing_to_empty_does_not_open_companion(self) -> None:
        companion = Mock()
        lifecycle = AgentLifecycle(companion, watch_codex_input=True)

        lifecycle.child_output(b"Ask Codex to do anything")
        lifecycle.user_input(b"oops")
        lifecycle.user_input(b"\x7f\x7f\x7f\x7f")
        lifecycle.user_input(b"\r")
        companion.show.assert_not_called()

    def test_codex_slash_commands_do_not_open_companion(self) -> None:
        companion = Mock()
        lifecycle = AgentLifecycle(companion, watch_codex_input=True)
        lifecycle.child_output(b"Ask Codex to do anything")

        lifecycle.user_input(b"/status\r")
        companion.show.assert_not_called()

        # Editing the "/" away makes it an ordinary prompt again.
        lifecycle.user_input(b"/x\x7f\x7ffix it\r")
        companion.show.assert_called_once_with()

    def test_claude_input_does_not_use_enter_fallback(self) -> None:
        companion = Mock()
        lifecycle = AgentLifecycle(companion)

        lifecycle.user_input(b"first prompt\r")

        companion.show.assert_not_called()

    def test_recognizes_ansi_fragmented_codex_prompt(self) -> None:
        rendered = b"".join(
            b"\x1b[;m\x1b[K  " + bytes([character]) + b"\x1b[m"
            for character in b"Ask Codex to do anything"
        )
        self.assertTrue(_contains_codex_prompt(rendered))
        self.assertFalse(_contains_codex_prompt(b"Continue anyway? [y/N]:"))

    def test_codex_prompt_can_be_detected_across_output_chunks(self) -> None:
        companion = Mock()
        lifecycle = AgentLifecycle(companion, watch_codex_input=True)

        lifecycle.child_output(b"Ask Codex to do")
        lifecycle.child_output(b" anything")
        lifecycle.user_input(b"fix the tests\n")

        companion.show.assert_called_once_with()

    def test_codex_notification_hides_companion_across_chunks(self) -> None:
        companion = Mock()
        lifecycle = AgentLifecycle(companion)
        lifecycle.child_output(b"output\x1b]9;Codex turn")
        companion.hide.assert_not_called()
        lifecycle.child_output(b" complete\x07more")
        companion.hide.assert_called_once_with()

    def test_completed_notification_does_not_close_next_prompt(self) -> None:
        companion = Mock()
        lifecycle = AgentLifecycle(companion, watch_codex_input=True)
        lifecycle.child_output(b"Ask Codex to do anything")

        lifecycle.user_input(b"first prompt\r")
        lifecycle.child_output(b"\x1b]9;turn complete\x07")
        lifecycle.user_input(b"second prompt\r")
        lifecycle.child_output(b"working on the second prompt")

        self.assertEqual(companion.show.call_count, 2)
        companion.hide.assert_called_once_with()

    def test_recognizes_both_osc_terminators(self) -> None:
        self.assertTrue(_contains_osc9(b"\x1b]9;done\x07"))
        self.assertTrue(_contains_osc9(b"\x1b]9;done\x1b\\"))
        self.assertFalse(_contains_osc9(b"\x1b]8;link\x07"))

    def test_prepares_codex_notification_settings(self) -> None:
        command = prepare_agent_command(["codex", "--model", "test"], "codex", Mock())
        self.assertEqual(command[0], "codex")
        self.assertEqual(command[-2:], ["--model", "test"])
        self.assertIn('tui.notification_method="osc9"', command)
        self.assertIn('tui.notification_condition="always"', command)

    def test_prepares_claude_http_hooks(self) -> None:
        companion = Mock()
        companion.lifecycle_url.side_effect = lambda event: f"http://local/{event}"
        command = prepare_agent_command(["claude", "--verbose"], "claude", companion)
        settings = json.loads(command[2])

        self.assertEqual(command[:2], ["claude", "--settings"])
        self.assertEqual(command[-1], "--verbose")
        hooks = settings["hooks"]
        self.assertEqual(
            hooks["UserPromptSubmit"][0]["hooks"][0]["url"],
            "http://local/start",
        )
        self.assertEqual(hooks["Stop"][0]["hooks"][0]["url"], "http://local/stop")
        self.assertEqual(hooks["PermissionRequest"][0]["hooks"][0]["url"], "http://local/stop")


class ComposerTrackerTests(unittest.TestCase):
    def test_reports_submitted_text_and_that_the_cursor_was_at_the_end(self) -> None:
        tracker = ComposerTracker()
        self.assertEqual(tracker.feed(b"fix the bug\r"), 1)
        self.assertEqual(tracker.submitted, [(b"fix the bug", True)])

    def test_pasted_text_and_multiline_prompts_are_kept(self) -> None:
        tracker = ComposerTracker()
        tracker.feed(b"\x1b[200~line one\nline two\x1b[201~\r")
        self.assertEqual(tracker.submitted, [(b"line one\nline two", True)])

    def test_moving_the_cursor_marks_the_prompt_as_not_at_the_end(self) -> None:
        for keys in (b"\x1b[D", b"\x1b[H", b"\x01", b"\x1bb", b"\x1b[3~"):
            with self.subTest(keys=keys):
                tracker = ComposerTracker()
                tracker.feed(b"some prompt" + keys + b"\r")
                self.assertFalse(tracker.submitted[0][1])

    def test_at_end_recovers_after_the_box_is_emptied(self) -> None:
        tracker = ComposerTracker()
        tracker.feed(b"abc\x1b[D\x15next prompt\r")
        self.assertEqual(tracker.submitted, [(b"next prompt", True)])

    def test_slash_commands_are_not_prompts(self) -> None:
        tracker = ComposerTracker()
        self.assertEqual(tracker.feed(b"/model\r"), 0)
        self.assertEqual(tracker.submitted, [])


if __name__ == "__main__":
    unittest.main()
