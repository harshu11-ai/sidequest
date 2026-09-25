"""Judge which model tier a prompt needs, using TypeSafe's Jev model."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

Tier = Literal["fast", "balanced", "deep"]
TIERS: tuple[Tier, ...] = ("fast", "balanced", "deep")

API_KEY_ENV = "TYPESAFE_API_KEY"
# Only the start and end of a long prompt are sent: enough to judge the task,
# and it bounds both latency and how much of a prompt leaves the machine.
_MAX_PROMPT_CHARS = 4000

_INSTRUCTIONS = (
    "A developer is about to send this prompt to an AI coding agent. "
    "Which model tier is the right fit for this specific request?"
)
_CRITERIA = {
    "fast": (
        "Small mechanical work: rename, format, tiny edit, a one-line question, "
        "explain a snippet, run a command, git housekeeping, simple lookup."
    ),
    "balanced": (
        "Ordinary engineering: implement a well-specified feature, fix a bug in known "
        "code, write tests, review a diff, modest multi-file changes."
    ),
    "deep": (
        "Hard reasoning: architecture or design decisions, large cross-cutting "
        "refactors, subtle or unexplained bugs, security or concurrency analysis, "
        "ambiguous open-ended tasks."
    ),
}


@dataclass(frozen=True, slots=True)
class Judgment:
    tier: Tier
    confidence: float


class JevClassifier:
    """Ask Jev for a tier. Any failure yields None: routing must never break a prompt."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 1.0,
        client: Any = None,
        question: Any = None,
    ) -> None:
        self._client = client
        self._question: Any = question  # built from the SDK on first use unless given
        if client is None:
            self._client = self._build_client(api_key or os.environ.get(API_KEY_ENV), timeout)

    @property
    def available(self) -> bool:
        return self._client is not None

    def classify(self, prompt: str) -> Judgment | None:
        if self._client is None:
            return None
        try:
            result = self._client.system_one(_clip(prompt), self._get_question())
            answer = result.choices["tier"]
            tier, confidence = answer.choice, float(answer.confidence)
        except Exception:  # noqa: BLE001 - network, auth, schema: all mean "no judgment"
            return None
        if tier not in TIERS:
            return None
        return Judgment(tier=tier, confidence=confidence)

    def _get_question(self) -> dict[str, Any]:
        if self._question is None:
            from typesafe_sdk import Choice

            self._question = {"tier": Choice(instructions=_INSTRUCTIONS, criteria=_CRITERIA)}
        return self._question

    @staticmethod
    def _build_client(api_key: str | None, timeout: float) -> Any:
        if not api_key:
            return None
        try:
            from typesafe_sdk import RetryPolicy, TypeSafeClient
        except ImportError:
            return None
        return TypeSafeClient(
            api_key=api_key,
            retry=RetryPolicy(max_retries=0, timeout=timeout),
        )


def _clip(prompt: str) -> str:
    if len(prompt) <= _MAX_PROMPT_CHARS:
        return prompt
    half = _MAX_PROMPT_CHARS // 2
    return f"{prompt[:half]}\n[...]\n{prompt[-half:]}"
