"""Which model each tier means, by the name the agent's own /model picker shows."""

from __future__ import annotations

from sidequest.routing.classifier import Tier

# Picker names change as vendors ship models: override them under "routing" in
# the config file rather than relying on these staying current.
DEFAULT_MODELS: dict[str, dict[Tier, str]] = {
    "claude": {"fast": "Haiku 4.5", "balanced": "Sonnet 5", "deep": "Opus 5.5"},
    "codex": {"fast": "GPT-6-Luna", "balanced": "GPT-6-Sol", "deep": "GPT-6-Astra"},
}
