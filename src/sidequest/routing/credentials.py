"""Where the TypeSafe API key comes from: the environment, or a private key file."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

API_KEY_ENV = "TYPESAFE_API_KEY"
KEY_FILE_NAME = "typesafe_key"


class CredentialsError(RuntimeError):
    """The key file exists but cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ApiKey:
    value: str
    source: str  # the environment variable's name, or the key file's path
    from_environment: bool


def key_file_path(config_path: Path) -> Path:
    """The key file lives beside the config file."""
    return config_path.parent / KEY_FILE_NAME


def resolve_api_key(config_path: Path) -> ApiKey | None:
    """The environment variable wins; otherwise the key file, if there is one."""
    from_environment = os.environ.get(API_KEY_ENV, "").strip()
    if from_environment:
        return ApiKey(from_environment, API_KEY_ENV, from_environment=True)

    path = key_file_path(config_path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CredentialsError(f"could not read {path}: {error}") from error
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialsError(f"{path} can be read by other users; run: chmod 600 {path}")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise CredentialsError(f"could not read {path}: {error}") from error
    return ApiKey(value, str(path), from_environment=False) if value else None


def save_api_key(config_path: Path, key: str) -> Path:
    """Write *key* to the key file, readable only by the current user."""
    path = key_file_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file 0600, so the key is never briefly world-readable.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".typesafe_key.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(key.strip() + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path
