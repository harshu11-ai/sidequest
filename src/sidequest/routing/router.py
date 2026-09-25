"""Turn a prompt into a decision to switch models, or to leave the model alone."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from sidequest.routing.classifier import TIERS, Judgment, Tier

# Below this, a prompt is usually a reply ("yes", "go ahead", "make it faster")
# whose difficulty depends on the conversation, which the classifier never sees.
_MIN_PROMPT_CHARS = 15

DEFAULT_MIN_CONFIDENCE = 0.7


class Classifier(Protocol):
    def classify(self, prompt: str) -> Judgment | None: ...


class ModelRouter:
    """Pick the model for each prompt, switching only when it clearly helps.

    A switch can cost the conversation its prompt cache, so the router stays put
    unless the classifier is confident *and* the tier differs from the current one.
    """

    def __init__(
        self,
        classifier: Classifier,
        models: Mapping[Tier, str],
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        missing = [tier for tier in TIERS if tier not in models]
        if missing:
            raise ValueError(f"no model configured for tier(s): {', '.join(missing)}")
        self._classifier = classifier
        self._models = dict(models)
        self._min_confidence = min_confidence
        self.current_tier: Tier | None = None

    def decide(self, prompt: str) -> str | None:
        """Return the model to switch to for *prompt*, or None to keep the current one."""
        if len(prompt.strip()) < _MIN_PROMPT_CHARS:
            return None
        judgment = self._classifier.classify(prompt)
        if judgment is None or judgment.confidence < self._min_confidence:
            return None
        if judgment.tier == self.current_tier:
            return None
        return self._models[judgment.tier]

    def switched(self, model: str) -> None:
        """Record that the agent is now on *model*; call only once the switch took effect."""
        for tier, name in self._models.items():
            if name == model:
                self.current_tier = tier
                return
