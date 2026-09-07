"""High-precision, context-free token correction."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from threading import Event, Thread
from typing import Protocol

COMMON_TYPOS: dict[str, str] = {
    "acheive": "achieve",
    "adress": "address",
    "adn": "and",
    "becuase": "because",
    "canddiate": "candidate",
    "canddiates": "candidates",
    "definately": "definitely",
    "enviroment": "environment",
    "fucntion": "function",
    "mes": "me",
    "occured": "occurred",
    "recieve": "receive",
    "repsonse": "response",
    "seperate": "separate",
    "taht": "that",
    "teh": "the",
}

COMMON_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "another",
    "because",
    "before",
    "between",
    "change",
    "check",
    "could",
    "does",
    "file",
    "find",
    "first",
    "fix",
    "from",
    "function",
    "have",
    "into",
    "like",
    "make",
    "need",
    "other",
    "please",
    "project",
    "should",
    "that",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "through",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "write",
}

_TOKEN_PARTS = re.compile(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def _is_adjacent_transposition(original: str, candidate: str) -> bool:
    """Return whether candidate swaps exactly one neighboring character pair."""
    if len(original) != len(candidate) or original == candidate:
        return False

    differences = [
        index
        for index, (left, right) in enumerate(zip(original, candidate, strict=True))
        if left != right
    ]
    if len(differences) != 2:
        return False

    first, second = differences
    return (
        second == first + 1
        and original[first] == candidate[second]
        and original[second] == candidate[first]
    )


@dataclass(frozen=True, slots=True)
class Correction:
    """A replacement selected by the correction engine."""

    original: str
    replacement: str
    reason: str


class CorrectionEngine(Protocol):
    """Interface implemented by interchangeable correction engines."""

    def suggest(self, token: str) -> Correction | None:
        """Return a correction for a complete token, or abstain."""
        ...


class ConservativeCorrector:
    """Apply explicit mappings and conservative context-free corrections."""

    def __init__(
        self,
        custom_corrections: Mapping[str, str] | None = None,
        abbreviations: Mapping[str, str] | None = None,
    ) -> None:
        self.typos = {**COMMON_TYPOS, **(custom_corrections or {})}
        self.abbreviations = dict(abbreviations or {})

    def suggest(self, token: str) -> Correction | None:
        """Return a correction for a complete token, or abstain."""
        if self._looks_technical(token):
            return None

        match = _TOKEN_PARTS.fullmatch(token)
        if match is None:
            return None

        prefix, word, suffix = match.groups()
        if not word.islower():
            return None

        replacement = self.abbreviations.get(word)
        reason = "personal abbreviation"
        if replacement is None:
            replacement = self.typos.get(word)
            reason = "common typo"
        if replacement is None:
            replacement = self._unique_transposition(word)
            reason = "adjacent transposition"
        if replacement is None:
            replacement = self._unique_extra_character(word)
            reason = "single extra character"

        if replacement is None or replacement == word:
            return None

        return Correction(
            original=token,
            replacement=f"{prefix}{replacement}{suffix}",
            reason=reason,
        )

    @staticmethod
    def _looks_technical(token: str) -> bool:
        if not token:
            return True
        if token.startswith(("-", "~", ".", "/")):
            return True
        if _URL_SCHEME.match(token):
            return True
        if "@" in token or "/" in token or "\\" in token:
            return True
        if "_" in token or "-" in token:
            return True
        if any(character.isdigit() for character in token):
            return True
        if any(character.isupper() for character in token):
            return True
        # Multiple dots usually indicate a filename, hostname, or versioned name.
        return token.count(".") > 1

    @staticmethod
    def _unique_transposition(word: str) -> str | None:
        candidates: set[str] = set()
        characters = list(word)
        for index in range(len(characters) - 1):
            if characters[index] == characters[index + 1]:
                continue
            swapped = characters.copy()
            swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
            candidate = "".join(swapped)
            if candidate in COMMON_WORDS:
                candidates.add(candidate)
        if len(candidates) == 1:
            return candidates.pop()
        return None

    @staticmethod
    def _unique_extra_character(word: str) -> str | None:
        candidates = {
            word[:index] + word[index + 1 :]
            for index in range(len(word))
            if word[:index] + word[index + 1 :] in COMMON_WORDS
        }
        if len(candidates) == 1:
            return candidates.pop()
        return None


class FrequencyCorrector:
    """Add strict frequency-ranked spelling suggestions to the rule engine."""

    MINIMUM_CANDIDATE_COUNT = 1_000_000
    MINIMUM_FREQUENCY_MARGIN = 10

    def __init__(
        self,
        *,
        background: bool = True,
        custom_corrections: Mapping[str, str] | None = None,
        abbreviations: Mapping[str, str] | None = None,
    ) -> None:
        self.rules = ConservativeCorrector(custom_corrections, abbreviations)
        self._sym_spell: object | None = None
        self._ready = Event()
        self._load_error: Exception | None = None
        if background:
            Thread(target=self._load_dictionary, name="autocorrect-dictionary", daemon=True).start()
        else:
            self._load_dictionary()

    def suggest(self, token: str) -> Correction | None:
        """Apply fast rules first, then a high-confidence dictionary lookup."""
        rule_correction = self.rules.suggest(token)
        if rule_correction is not None:
            return rule_correction
        if self.rules._looks_technical(token):
            return None

        match = _TOKEN_PARTS.fullmatch(token)
        if match is None:
            return None
        prefix, word, suffix = match.groups()
        if not word.islower() or len(word) < 4 or len(word) > 32:
            return None

        sym_spell = self._sym_spell
        if sym_spell is None or word in sym_spell.words:
            return None

        from symspellpy import Verbosity

        max_distance = 2 if len(word) >= 8 else 1
        suggestions = sym_spell.lookup(
            word,
            Verbosity.ALL,
            max_edit_distance=max_distance,
            include_unknown=False,
        )
        if not suggestions:
            return None

        minimum_distance = min(item.distance for item in suggestions)
        nearest = [item for item in suggestions if item.distance == minimum_distance]
        nearest.sort(key=lambda item: item.count, reverse=True)
        best = nearest[0]
        if best.count < self.MINIMUM_CANDIDATE_COUNT:
            return None
        top_ranked_transposition = _is_adjacent_transposition(word, best.term)
        if (
            not top_ranked_transposition
            and len(nearest) > 1
            and best.count < nearest[1].count * self.MINIMUM_FREQUENCY_MARGIN
        ):
            return None

        return Correction(
            original=token,
            replacement=f"{prefix}{best.term}{suffix}",
            reason=(
                "frequency-ranked transposition"
                if top_ranked_transposition
                else "frequency-ranked spelling"
            ),
        )

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        """Wait for dictionary loading; primarily useful for tests and diagnostics."""
        return self._ready.wait(timeout) and self._sym_spell is not None

    @property
    def load_error(self) -> Exception | None:
        return self._load_error

    def _load_dictionary(self) -> None:
        try:
            from symspellpy import SymSpell

            sym_spell = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
            dictionary_path = files("symspellpy") / "frequency_dictionary_en_82_765.txt"
            if not sym_spell.load_dictionary(dictionary_path, 0, 1):
                raise RuntimeError("could not load the bundled English frequency dictionary")
            self._sym_spell = sym_spell
        except Exception as error:  # The curated engine remains available if loading fails.
            self._load_error = error
        finally:
            self._ready.set()


@lru_cache(maxsize=1)
def get_default_corrector() -> FrequencyCorrector:
    """Return the process-wide engine and load its dictionary once."""
    return FrequencyCorrector(background=True)
