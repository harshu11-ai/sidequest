import json
import unittest
from unittest.mock import Mock

from sidequest.lifecycle import (
    AgentLifecycle,
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


if __name__ == "__main__":
    unittest.main()
