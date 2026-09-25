import unittest
from types import SimpleNamespace

from sidequest.routing.classifier import JevClassifier, Judgment
from sidequest.routing.router import ModelRouter

MODELS = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}
PROMPT = "please look into why the relay drops players on reconnect"
QUESTION = {"tier": object()}  # stands in for the SDK's Choice


class FakeClassifier:
    def __init__(self, judgment: Judgment | None) -> None:
        self.judgment = judgment
        self.calls: list[str] = []

    def classify(self, prompt: str) -> Judgment | None:
        self.calls.append(prompt)
        return self.judgment


class FakeClient:
    def __init__(
        self,
        choice: str = "deep",
        confidence: float = 0.9,
        error: Exception | None = None,
    ) -> None:
        self.choice = choice
        self.confidence = confidence
        self.error = error
        self.states: list[str] = []

    def system_one(self, state: str, questions: dict) -> SimpleNamespace:
        self.states.append(state)
        if self.error is not None:
            raise self.error
        answer = SimpleNamespace(choice=self.choice, confidence=self.confidence)
        return SimpleNamespace(choices={"tier": answer})


class ModelRouterTests(unittest.TestCase):
    def router(self, judgment: Judgment | None, **kwargs) -> tuple[ModelRouter, FakeClassifier]:
        classifier = FakeClassifier(judgment)
        return ModelRouter(classifier, MODELS, **kwargs), classifier

    def test_switches_to_the_confident_tier(self) -> None:
        router, _ = self.router(Judgment("deep", 0.95))
        self.assertEqual(router.decide(PROMPT), "opus")

    def test_low_confidence_keeps_the_current_model(self) -> None:
        router, _ = self.router(Judgment("deep", 0.4))
        self.assertIsNone(router.decide(PROMPT))

    def test_no_judgment_keeps_the_current_model(self) -> None:
        router, _ = self.router(None)
        self.assertIsNone(router.decide(PROMPT))

    def test_same_tier_is_not_switched_again(self) -> None:
        router, _ = self.router(Judgment("deep", 0.95))
        router.switched("opus")
        self.assertIsNone(router.decide(PROMPT))

    def test_switches_back_when_the_tier_changes(self) -> None:
        router, classifier = self.router(Judgment("deep", 0.95))
        router.switched("opus")
        classifier.judgment = Judgment("fast", 0.95)
        self.assertEqual(router.decide(PROMPT), "haiku")

    def test_decide_alone_does_not_change_state(self) -> None:
        router, _ = self.router(Judgment("deep", 0.95))
        self.assertEqual(router.decide(PROMPT), "opus")
        self.assertEqual(router.decide(PROMPT), "opus")

    def test_short_replies_are_not_classified(self) -> None:
        router, classifier = self.router(Judgment("fast", 0.99))
        self.assertIsNone(router.decide("yes"))
        self.assertIsNone(router.decide("   go ahead   "))
        self.assertEqual(classifier.calls, [])

    def test_requires_a_model_for_every_tier(self) -> None:
        with self.assertRaises(ValueError):
            ModelRouter(FakeClassifier(None), {"fast": "haiku"})

    def test_switched_ignores_unknown_models(self) -> None:
        router, _ = self.router(Judgment("deep", 0.95))
        router.switched("something-else")
        self.assertIsNone(router.current_tier)


class JevClassifierTests(unittest.TestCase):
    def test_returns_the_tier_and_confidence(self) -> None:
        classifier = JevClassifier(question=QUESTION, client=FakeClient("balanced", 0.82))
        self.assertEqual(classifier.classify(PROMPT), Judgment("balanced", 0.82))

    def test_any_client_error_means_no_judgment(self) -> None:
        classifier = JevClassifier(question=QUESTION, client=FakeClient(error=TimeoutError("slow")))
        self.assertIsNone(classifier.classify(PROMPT))

    def test_unknown_tier_means_no_judgment(self) -> None:
        classifier = JevClassifier(question=QUESTION, client=FakeClient(choice="ultra"))
        self.assertIsNone(classifier.classify(PROMPT))

    def test_long_prompts_are_clipped_keeping_both_ends(self) -> None:
        client = FakeClient()
        prompt = "START" + "x" * 10_000 + "END"
        JevClassifier(question=QUESTION, client=client).classify(prompt)
        sent = client.states[0]
        self.assertLess(len(sent), 4100)
        self.assertTrue(sent.startswith("START"))
        self.assertTrue(sent.endswith("END"))

    def test_without_a_key_it_is_unavailable(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TYPESAFE_API_KEY", None)
            classifier = JevClassifier()
        self.assertFalse(classifier.available)
        self.assertIsNone(classifier.classify(PROMPT))


if __name__ == "__main__":
    unittest.main()
