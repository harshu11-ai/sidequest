import unittest
from unittest.mock import patch

from sidequest.routing.screen import Picker, PickerEntry, VirtualScreen, parse_picker
from sidequest.routing.switcher import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    ModelSwitcher,
    PromptNotRestored,
    SessionPreferences,
    SwitchError,
    SwitchTimeouts,
)

CLAUDE_SCREEN = """\
 ▐▛███▛█   Claude Code v2.1.282
   Select model
   Switch between Claude models. Your pick becomes the default for new sessions.
     1.  Default (recommended)  Haiku 4.5
     2.  Opus 5.5               Most capable for ambitious work
   ❯ 3.  Sonnet 5 ✔             Most efficient for everyday tasks
     4.  Fable 5.1              For your toughest challenges
     5.  Haiku 4.5              Fastest for quick answers
   ↓ 10. Opus 4.6               Best for everyday, complex tasks
   Enter to set as default · s to use this session only · Esc to cancel
""".splitlines()

CODEX_SCREEN = """\
  Select Model and Effort
  1. GPT-6-Astra (default)  Frontier intelligence for the most demanding work.
  2. GPT-6-Sol              Workhorse model for coding and everyday work.
  3. GPT-6-Luna             Fast and affordable model for easier tasks.
› 4. GPT-5.6-Sol (current)  Older coding model for complex work.
  enter select · esc back
""".splitlines()


class PickerParsingTests(unittest.TestCase):
    def test_reads_claude_picker(self) -> None:
        picker = parse_picker(CLAUDE_SCREEN)
        assert picker is not None
        self.assertEqual(picker.title, "Select model")
        self.assertEqual(picker.cursor.label, "Sonnet 5")
        self.assertTrue(picker.cursor.current)
        self.assertEqual(picker.find("haiku 4.5").number, 5)
        self.assertEqual(picker.find("Opus 5.5").number, 2)
        self.assertIsNone(picker.find("Opus 5"))  # not confused with Opus 5.5
        self.assertEqual(picker.entries[-1].number, 10)

    def test_reads_codex_picker(self) -> None:
        picker = parse_picker(CODEX_SCREEN)
        assert picker is not None
        self.assertEqual(picker.cursor.label, "GPT-5.6-Sol")
        self.assertTrue(picker.cursor.current)
        self.assertEqual(picker.find("GPT-6-Sol").number, 2)
        self.assertEqual(picker.find("GPT-6-Astra").label, "GPT-6-Astra")

    def test_no_picker_on_an_ordinary_screen(self) -> None:
        self.assertIsNone(parse_picker(["❯ 3 files changed", "Select the best option"]))

    @unittest.skipUnless(VirtualScreen.available(), "needs pyte (the routing extra)")
    def test_virtual_screen_renders_cursor_movement_and_modes(self) -> None:
        screen = VirtualScreen(rows=10, columns=40)
        screen.feed(b"\x1b[?2004h\x1b[2;3Hhello\x1b[3;1H\xe2\x9d\xaf\x1b[6G1.  Default")
        self.assertTrue(screen.bracketed_paste)
        self.assertIn("  hello", screen.text())
        self.assertIn("❯    1.  Default", screen.text())


class FakeScreen:
    bracketed_paste = True

    def __init__(self, agent: "FakeAgent") -> None:
        self.agent = agent

    def picker(self) -> Picker | None:
        return self.agent.picker()

    def text(self) -> str:
        return self.agent.text()

    def permission_mode(self) -> str | None:
        return self.agent.mode

    def reasoning_effort(self) -> str | None:
        return self.agent.effort

    def lines(self) -> list[str]:
        return self.agent.lines()


class FakeAgent:
    """Behaves like the /model pickers of Claude Code and Codex, as observed."""

    def __init__(self, application: str, *, models=None, current: str = "Sonnet 5") -> None:
        self.application = application
        self.models = models or ["Default", "Opus 5.5", "Sonnet 5", "Fable 5.1", "Haiku 4.5"]
        self.current = current
        self.cursor = self.models.index(current)
        self.stage: str | None = None  # None, "model" or "reasoning"
        self.box = ""
        self.message = ""
        self.sent: list[bytes] = []
        self.saved_default = False
        self.session_model: str | None = None
        self.pending: str | None = None
        self.mode: str | None = None
        self.no_auto: set[str] = set()  # models with no auto mode
        self.pastes_to_drop = 0  # an agent that is still redrawing loses early pastes
        self.effort: str | None = None  # Codex's reasoning effort, as its footer shows it
        self.effort_cursor = 1  # the reasoning screen opens on the model's default: Medium
        self.cache_dialog = False  # ask "Switch model?" before applying, once there is history
        self.dialog_open = False
        self.switches_confirmed = 0

    def _cycle(self) -> list[str]:
        modes = ["manual mode on", "accept edits on", "plan mode on"]
        return modes if self.current in self.no_auto else ["auto mode on", *modes]

    def write(self, data: bytes) -> None:
        self.sent.append(data)
        if data == b"\x1b[Z" and self.mode is not None:
            cycle = self._cycle()
            position = cycle.index(self.mode) if self.mode in cycle else -1
            self.mode = cycle[(position + 1) % len(cycle)]
        elif data.startswith(BRACKETED_PASTE_START) and self.pastes_to_drop:
            self.pastes_to_drop -= 1
        elif data.startswith(BRACKETED_PASTE_START):
            self.box += data[len(BRACKETED_PASTE_START) : -len(BRACKETED_PASTE_END)].decode()
        elif self.stage is None:
            self._idle_key(data)
        else:
            self._picker_key(data)

    def _idle_key(self, data: bytes) -> None:
        if self.dialog_open:
            if data == b"\r":
                self.dialog_open = False
                self._apply(self.pending_model)
            return
        if data == b"\r" and self.box == "/model":
            self.box, self.stage, self.cursor = "", "model", self.models.index(self.current)
        elif data == b"\x7f" * len(data):
            self.box = self.box[: len(self.box) - len(data)]
        else:
            self.box += data.decode()

    def _picker_key(self, data: bytes) -> None:
        if data in (b"j", b"k") and self.stage == "reasoning":
            step = 1 if data == b"j" else -1
            self.effort_cursor = max(0, min(2, self.effort_cursor + step))
        elif data in (b"j", b"k"):
            step = 1 if data == b"j" else -1
            self.cursor = max(0, min(len(self.models) - 1, self.cursor + step))
        elif data == b"\x1b":
            back_to_models = self.stage == "reasoning" and self.application == "codex"
            self.stage = "model" if back_to_models else None
        elif data == b"s" and (self.application == "claude" or self.stage == "reasoning"):
            chosen = self.pending or self.models[self.cursor]
            if self.stage == "reasoning":
                self.effort = ("low", "medium", "high")[self.effort_cursor]
                self.effort_cursor = 1  # the next reasoning screen opens on the default again
            self.stage = None
            if self.cache_dialog:
                self.dialog_open, self.pending_model = True, chosen
            else:
                self._apply(chosen)
        elif data == b"\r" and self.application == "codex" and self.stage == "model":
            self.pending, self.stage = self.models[self.cursor], "reasoning"
        else:
            self.saved_default = True  # Enter or a digit: the pickers persist these

    def _apply(self, model: str) -> None:
        self.session_model = model
        self.message += f"\nSet model to {model} for this session only"
        self.current = model
        if self.mode == "auto mode on" and self.current in self.no_auto:
            self.mode = "manual mode on"  # what Claude Code does

    def pump(self, seconds: float) -> None:
        pass

    def picker(self) -> Picker | None:
        if self.stage is None:
            return None
        title = "Select model" if self.stage == "model" else "Select Reasoning Level for X"
        if self.stage == "reasoning":
            entries = [
                PickerEntry(i + 1, name, i == self.effort_cursor)
                for i, name in enumerate(("Low", "Medium", "High"))
            ]
        else:
            entries = [
                PickerEntry(i + 1, name, i == self.cursor, name == self.current)
                for i, name in enumerate(self.models)
            ]
        return Picker(title, tuple(entries))

    def lines(self) -> list[str]:
        return self.text().split("\n")

    def text(self) -> str:
        if self.dialog_open:
            return "Switch model?\n❯ 1. Yes, switch to X\n  2. No, go back\n"
        return f"{self.message}\n❯ {self.box}"


def codex_agent() -> FakeAgent:
    models = ["GPT-6-Astra", "GPT-6-Sol", "GPT-6-Luna"]
    return FakeAgent("codex", models=models, current="GPT-6-Sol")


def make_switcher(application: str, agent: FakeAgent) -> ModelSwitcher:
    timeouts = SwitchTimeouts(open_picker=0.2, step=0.2)
    return ModelSwitcher(application, FakeScreen(agent), agent, timeouts=timeouts)


class ModelSwitcherTests(unittest.TestCase):
    def test_claude_switches_for_this_session_only(self) -> None:
        agent = FakeAgent("claude")
        agent.box = "fix the parser"
        self.assertTrue(make_switcher("claude", agent).switch("Haiku 4.5", b"fix the parser"))
        self.assertEqual(agent.session_model, "Haiku 4.5")
        self.assertFalse(agent.saved_default)
        self.assertEqual(agent.box, "fix the parser")  # put back for the user's Enter

    def test_codex_switches_for_this_session_only(self) -> None:
        agent = codex_agent()
        agent.box = "hello there"
        self.assertTrue(make_switcher("codex", agent).switch("GPT-6-Luna", b"hello there"))
        self.assertEqual(agent.session_model, "GPT-6-Luna")
        self.assertFalse(agent.saved_default)
        self.assertEqual(agent.box, "hello there")

    def test_moves_up_as_well_as_down(self) -> None:
        agent = FakeAgent("claude")
        make_switcher("claude", agent).switch("Opus 5.5", b"")
        self.assertEqual(agent.session_model, "Opus 5.5")

    def test_already_on_the_model_changes_nothing(self) -> None:
        agent = FakeAgent("claude")
        agent.box = "some prompt"
        self.assertFalse(make_switcher("claude", agent).switch("Sonnet 5", b"some prompt"))
        self.assertIsNone(agent.session_model)
        self.assertIsNone(agent.stage)
        self.assertEqual(agent.box, "some prompt")

    def test_unknown_model_cancels_and_restores_the_prompt(self) -> None:
        agent = FakeAgent("claude")
        agent.box = "some prompt"
        with self.assertRaises(SwitchError):
            make_switcher("claude", agent).switch("Nonexistent 9", b"some prompt")
        self.assertIsNone(agent.stage)
        self.assertFalse(agent.saved_default)
        self.assertEqual(agent.box, "some prompt")

    def test_picker_that_never_opens_is_an_error(self) -> None:
        agent = FakeAgent("claude")
        agent.picker = lambda: None  # type: ignore[method-assign]
        with self.assertRaises(SwitchError):
            make_switcher("claude", agent).switch("Haiku 4.5", b"x")

    def test_never_sends_a_persisting_key(self) -> None:
        agent = FakeAgent("claude")
        make_switcher("claude", agent).switch("Haiku 4.5", b"prompt")
        picker_keys = agent.sent[agent.sent.index(b"\r") + 1 :]
        self.assertFalse({b"\r", b"\n", *(str(n).encode() for n in range(10))} & set(picker_keys))

    def test_codex_sends_exactly_one_enter_before_the_session_key(self) -> None:
        agent = codex_agent()
        make_switcher("codex", agent).switch("GPT-6-Luna", b"prompt")
        opened = agent.sent.index(b"\r")
        self.assertEqual(agent.sent[opened + 1 :].count(b"\r"), 1)
        self.assertLess(agent.sent.index(b"\r", opened + 1), agent.sent.index(b"s"))


class EffortTests(unittest.TestCase):
    def test_codex_keeps_the_reasoning_effort_the_user_had(self) -> None:
        agent, prefs = codex_agent(), SessionPreferences()
        agent.effort = "high"  # the default on offer is Medium
        agent.box = "prompt"
        timeouts = SwitchTimeouts(open_picker=0.2, step=0.2)
        switcher = ModelSwitcher("codex", FakeScreen(agent), agent, timeouts=timeouts, modes=prefs)
        with patch("sidequest.routing.switcher._SETTLE_S", 0.05):
            switcher.switch("GPT-6-Luna", b"prompt")
            self.assertEqual(agent.effort, "high")
            # The effort our own switch left is not a choice the user made.
            switcher.switch("GPT-6-Astra", b"prompt")
        self.assertEqual(agent.effort, "high")

    def test_an_effort_the_user_changed_in_between_is_respected(self) -> None:
        agent, prefs = codex_agent(), SessionPreferences()
        agent.effort = "high"
        timeouts = SwitchTimeouts(open_picker=0.2, step=0.2)
        switcher = ModelSwitcher("codex", FakeScreen(agent), agent, timeouts=timeouts, modes=prefs)
        with patch("sidequest.routing.switcher._SETTLE_S", 0.05):
            switcher.switch("GPT-6-Luna", b"prompt")
            agent.effort = "low"  # the user picked this themselves
            switcher.switch("GPT-6-Astra", b"prompt")
        self.assertEqual(agent.effort, "low")

    def test_claude_is_not_asked_about_effort(self) -> None:
        agent = FakeAgent("claude")
        agent.effort = "high"
        make_switcher("claude", agent).switch("Haiku 4.5", b"")
        self.assertEqual(agent.effort, "high")


class CacheDialogTests(unittest.TestCase):
    def test_answers_the_switch_confirmation_and_finishes_the_switch(self) -> None:
        agent = FakeAgent("claude")
        agent.message = "Set model to Haiku 4.5 for this session only"  # from an earlier switch
        agent.cache_dialog = True
        agent.box = "a follow-up question"
        with patch("sidequest.routing.switcher._SETTLE_S", 0.05):
            make_switcher("claude", agent).switch("Opus 5.5", b"a follow-up question")
        self.assertEqual(agent.session_model, "Opus 5.5")
        self.assertFalse(agent.dialog_open)
        self.assertEqual(agent.box, "a follow-up question")  # not lost to the dialog

    def test_an_old_confirmation_on_screen_is_not_mistaken_for_a_new_one(self) -> None:
        agent = FakeAgent("claude")
        agent.message = "Set model to Haiku 4.5 for this session only"
        agent.cache_dialog = True
        agent.write = lambda data: None  # type: ignore[method-assign]  # nothing responds
        with self.assertRaises(SwitchError):
            make_switcher("claude", agent).switch("Opus 5.5", b"prompt")


class PromptRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("_SETTLE_S", "_PASTE_WAIT_S"):
            patcher = patch(f"sidequest.routing.switcher.{name}", 0.05)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_dropped_paste_is_retried_without_duplicating_the_prompt(self) -> None:
        agent = FakeAgent("claude")
        agent.box = "explain the relay protocol"
        agent.pastes_to_drop = 1
        make_switcher("claude", agent).switch("Haiku 4.5", b"explain the relay protocol")
        self.assertEqual(agent.box, "explain the relay protocol")
        pastes = [d for d in agent.sent if d.startswith(BRACKETED_PASTE_START)]
        self.assertEqual(len(pastes), 2)

    def test_a_prompt_that_never_comes_back_is_reported_not_silently_lost(self) -> None:
        agent = FakeAgent("claude")
        agent.box = "explain the relay protocol"
        agent.pastes_to_drop = 5
        with self.assertRaises(PromptNotRestored):
            make_switcher("claude", agent).switch("Haiku 4.5", b"explain the relay protocol")
        self.assertEqual(agent.session_model, "Haiku 4.5")  # the switch itself did take

    def test_long_prompts_are_not_waited_on_because_the_agent_may_collapse_them(self) -> None:
        agent = FakeAgent("claude")
        long_prompt = ("word " * 200).encode()
        agent.box = long_prompt.decode()
        make_switcher("claude", agent).switch("Haiku 4.5", long_prompt)
        self.assertEqual(agent.box, long_prompt.decode())


class PermissionModeTests(unittest.TestCase):
    def switch(self, agent: FakeAgent, keeper: SessionPreferences, label: str) -> None:
        timeouts = SwitchTimeouts(open_picker=0.2, step=0.2)
        ModelSwitcher("claude", FakeScreen(agent), agent, timeouts=timeouts, modes=keeper).switch(
            label, b"prompt"
        )

    def test_auto_mode_is_restored_after_returning_to_a_model_that_has_it(self) -> None:
        agent, keeper = FakeAgent("claude"), SessionPreferences()
        agent.mode, agent.no_auto = "auto mode on", {"Haiku 4.5"}
        self.switch(agent, keeper, "Haiku 4.5")
        self.assertEqual(agent.mode, "manual mode on")  # Haiku has no auto mode
        self.switch(agent, keeper, "Sonnet 5")
        self.assertEqual(agent.mode, "auto mode on")

    def test_a_mode_the_user_chose_in_between_is_kept(self) -> None:
        agent, keeper = FakeAgent("claude"), SessionPreferences()
        agent.mode, agent.no_auto = "auto mode on", {"Haiku 4.5"}
        self.switch(agent, keeper, "Haiku 4.5")
        agent.mode = "plan mode on"  # the user cycled it themselves
        self.switch(agent, keeper, "Sonnet 5")
        self.assertEqual(agent.mode, "plan mode on")

    def test_gives_up_when_the_mode_is_not_on_offer_and_remembers_that(self) -> None:
        agent, keeper = FakeAgent("claude"), SessionPreferences()
        agent.mode, agent.no_auto = "auto mode on", {"Haiku 4.5"}
        self.switch(agent, keeper, "Haiku 4.5")
        self.assertEqual(agent.mode, "manual mode on")  # the cycle came back to where it began
        self.assertIn("Haiku 4.5", keeper.unavailable)

        self.switch(agent, keeper, "Sonnet 5")
        self.assertEqual(agent.mode, "auto mode on")
        presses = agent.sent.count(b"\x1b[Z")
        self.switch(agent, keeper, "Haiku 4.5")  # known not to work: no more attempts
        self.assertEqual(agent.sent.count(b"\x1b[Z"), presses)
        self.assertEqual(agent.mode, "manual mode on")

    def test_codex_never_touches_permission_modes(self) -> None:
        agent = codex_agent()
        agent.mode = "auto mode on"
        make_switcher("codex", agent).switch("GPT-6-Luna", b"prompt")
        self.assertNotIn(b"\x1b[Z", agent.sent)


if __name__ == "__main__":
    unittest.main()
