"""Validated user configuration for Sidequest."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_PLAIN_WORD = re.compile(r"^[a-z]+$")
_MAX_WORD_LENGTH = 64
_MAX_EXPANSION_LENGTH = 500


class ConfigurationError(ValueError):
    """Raised when a configuration file cannot be read or validated."""


@dataclass(frozen=True, slots=True)
class UserConfiguration:
    """A loaded configuration and the path it came from."""

    path: Path
    corrections: dict[str, str]
    abbreviations: dict[str, str]
    exists: bool


def default_config_path() -> Path:
    """Return the platform-neutral per-user configuration path."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "sidequest" / "config.json"


def legacy_config_path() -> Path:
    """Return the previous config path retained for automatic migration."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "cli-autocorrect" / "config.json"


def load_configuration(path: str | Path | None = None) -> UserConfiguration:
    """Load and validate a JSON configuration, or return an empty default."""
    config_path = Path(path).expanduser() if path is not None else default_config_path()
    if path is None and not config_path.exists() and legacy_config_path().exists():
        config_path = legacy_config_path()
    if not config_path.exists():
        return UserConfiguration(
            path=config_path,
            corrections={},
            abbreviations={},
            exists=False,
        )
    if not config_path.is_file():
        raise ConfigurationError(f"configuration path is not a file: {config_path}")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"could not read {config_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"invalid JSON in {config_path} at line {error.lineno}, column {error.colno}"
        ) from error

    if not isinstance(data, dict):
        raise ConfigurationError(f"{config_path} must contain a JSON object")
    unknown_keys = set(data) - {"abbreviations", "corrections"}
    if unknown_keys:
        names = ", ".join(sorted(str(key) for key in unknown_keys))
        raise ConfigurationError(f"unknown configuration key(s) in {config_path}: {names}")

    corrections = _mapping(data, "corrections", config_path)
    abbreviations = _mapping(data, "abbreviations", config_path)

    validated_corrections: dict[str, str] = {}
    for original, replacement in corrections.items():
        if not isinstance(original, str) or not isinstance(replacement, str):
            raise ConfigurationError("correction keys and values must both be strings")
        if not _valid_word(original) or not _valid_word(replacement):
            raise ConfigurationError(
                "corrections must use lowercase ASCII letters only and be at most "
                f"{_MAX_WORD_LENGTH} characters"
            )
        if original == replacement:
            raise ConfigurationError(f"correction maps {original!r} to itself")
        validated_corrections[original] = replacement

    validated_abbreviations: dict[str, str] = {}
    for abbreviation, expansion in abbreviations.items():
        if not isinstance(abbreviation, str) or not isinstance(expansion, str):
            raise ConfigurationError("abbreviation keys and values must both be strings")
        if not _valid_word(abbreviation):
            raise ConfigurationError(
                "abbreviation keys must use lowercase ASCII letters only and be at most "
                f"{_MAX_WORD_LENGTH} characters"
            )
        if not _valid_expansion(expansion):
            raise ConfigurationError(
                "abbreviation values must be 1 to "
                f"{_MAX_EXPANSION_LENGTH} printable ASCII characters with no surrounding spaces"
            )
        if abbreviation == expansion:
            raise ConfigurationError(f"abbreviation {abbreviation!r} expands to itself")
        if abbreviation in validated_corrections:
            raise ConfigurationError(
                f"{abbreviation!r} cannot be both a correction and an abbreviation"
            )
        validated_abbreviations[abbreviation] = expansion

    return UserConfiguration(
        path=config_path,
        corrections=validated_corrections,
        abbreviations=validated_abbreviations,
        exists=True,
    )


def _valid_word(value: str) -> bool:
    return len(value) <= _MAX_WORD_LENGTH and _PLAIN_WORD.fullmatch(value) is not None


def _valid_expansion(value: str) -> bool:
    return (
        1 <= len(value) <= _MAX_EXPANSION_LENGTH
        and value == value.strip()
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _mapping(data: dict[object, object], key: str, config_path: Path) -> dict[object, object]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key!r} in {config_path} must be a JSON object")
    return value
