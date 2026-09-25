import unittest

from sidequest.routing.classifier import Judgment
from sidequest.routing.router import ModelRouter
from sidequest.routing.session import RoutingSession
from sidequest.routing.switcher import PromptNotRestored, SwitchError

MODELS = {"fast": "Haiku 4.5", "balanced": "Sonnet 5", "deep": "Opus 5.5"}
PROMPT = "why does the relay drop players when they reconnect?"


class FixedClassifier:
    def __init__(self, tier: str = "deep", confidence: float = 0.95) -> None:
        self.judgment = Judgment(tier, confidence)
        self.calls = 0

    def classify(self, prompt: str) -> Judgment:
        self.calls += 1
        return self.judgment


class FakeScreen:
    def __init__(self, text: str = "", bracketed_paste: bool = True) -> None:
        self._text = text
        self.bracketed_paste = bracketed_paste

    def text(self) -> str:
        return self._text

    def permission_mode(self) -> str | None:
        return None

    def reasoning_effort(self) -> str | None:
        return None

    def feed(self, data: bytes) -> None:
        pass

    def resize(self, rows: int, columns: int) -> None:
        pass


class RecordingIO:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def pump(self, seconds: float) -> None:
        pass


class FakeSwitcher:
    result: bool | Exception = True
    calls: list[tuple[str, bytes]] = []

    def __init__(self, application, screen, io, modes=None) -> None:
        pass

    def switch(self, label: str, prompt: bytes) -> bool:
        FakeSwitcher.calls.append((label, prompt))
        if isinstance(FakeSwitcher.result, Exception):
            raise FakeSwitcher.result
        return FakeSwitcher.result


def make_session(screen: FakeScreen | None = None, classifier: FixedClassifier | None = None):
    router = ModelRouter(classifier or FixedClassifier(), MODELS)
    return RoutingSession("claude", router, screen or FakeScreen()), router


class RoutingSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSwitcher.result = True
        FakeSwitcher.calls = []
        import sidequest.routing.session as module

        self._original = module.ModelSwitcher
        module.ModelSwitcher = FakeSwitcher  # type: ignore[misc]
        self.addCleanup(setattr, module, "ModelSwitcher", self._original)

    def test_typing_alone_does_nothing(self) -> None:
        session, _ = make_session()
        self.assertIsNone(session.intercept(PROMPT.encode(), RecordingIO()))
        self.assertEqual(FakeSwitcher.calls, [])

    def test_enter_switches_then_hands_back_only_the_enter(self) -> None:
        session, router = make_session()
        io = RecordingIO()
        session.intercept(PROMPT.encode(), io)
        remainder = session.intercept(b"\r", io)
        self.assertEqual(remainder, b"\r")
        self.assertEqual(FakeSwitcher.calls, [("Opus 5.5", PROMPT.encode())])
        self.assertEqual(session.switches, ["Opus 5.5"])
        self.assertEqual(router.current_tier, "deep")

    def test_prompt_and_enter_in_one_chunk_sends_the_prompt_first(self) -> None:
        session, _ = make_session()
        io = RecordingIO()
        remainder = session.intercept(PROMPT.encode() + b"\r", io)
        self.assertEqual(remainder, b"\r")
        self.assertEqual(io.writes, [PROMPT.encode()])
        self.assertEqual(FakeSwitcher.calls[0][1], PROMPT.encode())

    def test_no_switch_needed_sends_as_usual(self) -> None:
        session, _ = make_session(classifier=FixedClassifier(confidence=0.3))
        self.assertIsNone(session.intercept(PROMPT.encode() + b"\r", RecordingIO()))
        self.assertEqual(FakeSwitcher.calls, [])

    def test_never_switches_while_the_agent_is_busy(self) -> None:
        classifier = FixedClassifier()
        session, _ = make_session(FakeScreen("✻ Thinking… (esc to interrupt)"), classifier)
        self.assertIsNone(session.intercept(PROMPT.encode() + b"\r", RecordingIO()))
        self.assertEqual(classifier.calls, 0)  # not even sent to TypeSafe

    def test_codex_working_indicator_counts_as_busy(self) -> None:
        session, _ = make_session(FakeScreen("• Working (12s • Esc to interrupt)"))
        self.assertIsNone(session.intercept(PROMPT.encode() + b"\r", RecordingIO()))

    def test_needs_bracketed_paste_to_put_the_prompt_back(self) -> None:
        session, _ = make_session(FakeScreen(bracketed_paste=False))
        self.assertIsNone(session.intercept(PROMPT.encode() + b"\r", RecordingIO()))

    def test_cursor_moved_away_from_the_end_is_left_alone(self) -> None:
        session, _ = make_session()
        self.assertIsNone(session.intercept(PROMPT.encode() + b"\x1b[D\r", RecordingIO()))
        self.assertEqual(FakeSwitcher.calls, [])

    def test_slash_commands_and_empty_enter_are_ignored(self) -> None:
        session, _ = make_session()
        self.assertIsNone(session.intercept(b"/model\r", RecordingIO()))
        self.assertIsNone(session.intercept(b"\r", RecordingIO()))

    def test_kitty_enter_is_recognised(self) -> None:
        session, _ = make_session()
        remainder = session.intercept(PROMPT.encode() + b"\x1b[13u", RecordingIO())
        self.assertEqual(remainder, b"\x1b[13u")

    def test_failures_are_reported_and_switching_turns_itself_off(self) -> None:
        FakeSwitcher.result = SwitchError("not in the picker")
        session, router = make_session()
        for _ in range(2):
            self.assertEqual(session.intercept(PROMPT.encode() + b"\r", RecordingIO()), b"\r")
        self.assertFalse(session.enabled)
        self.assertIsNone(router.current_tier)
        self.assertIn("not in the picker", session.notes[0])
        self.assertIsNone(session.intercept(PROMPT.encode() + b"\r", RecordingIO()))
        self.assertEqual(len(FakeSwitcher.calls), 2)

    def test_a_lost_prompt_is_reported_but_the_switch_is_still_recorded(self) -> None:
        FakeSwitcher.result = PromptNotRestored("could not put the prompt back")
        session, router = make_session()
        remainder = session.intercept(PROMPT.encode() + b"\r", RecordingIO())
        self.assertEqual(remainder, b"")  # no Enter: it could answer some other dialog
        self.assertEqual(router.current_tier, "deep")
        self.assertIn("retype your prompt", session.notes[0])
        self.assertTrue(session.enabled)

    def test_already_on_the_model_is_not_reported_as_a_switch(self) -> None:
        FakeSwitcher.result = False
        session, router = make_session()
        session.intercept(PROMPT.encode() + b"\r", RecordingIO())
        self.assertEqual(session.switches, [])
        self.assertEqual(router.current_tier, "deep")


if __name__ == "__main__":
    unittest.main()
