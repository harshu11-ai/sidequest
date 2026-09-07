"""Guarded self-update support for pipx installations."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE_NAME = "sidequest"
LEGACY_PACKAGE_NAME = "cli-autocorrect"
EXECUTABLE_NAMES = {"sidequest", "cauto"}
UPDATE_SOURCE = "git+https://github.com/harshu11-ai/sidequest.git"


class UpdateError(RuntimeError):
    """Raised when the active installation cannot be updated safely."""


@dataclass(frozen=True, slots=True)
class UpdateResult:
    previous_version: str
    current_version: str


@dataclass(frozen=True, slots=True)
class _PipxInstallation:
    version: str


def update_with_pipx() -> UpdateResult:
    """Force-reinstall the active pipx-managed copy from the GitHub repository."""
    pipx = shutil.which("pipx")
    if pipx is None:
        raise UpdateError("pipx is not available on PATH; install pipx before updating")

    before = _find_active_installation(_load_pipx_listing(pipx))
    try:
        completed = subprocess.run(
            [pipx, "install", "--force", UPDATE_SOURCE],
            check=False,
        )
    except OSError as error:
        raise UpdateError(f"could not start pipx: {error}") from error
    if completed.returncode != 0:
        raise UpdateError(f"pipx exited with status {completed.returncode}")

    after = _find_active_installation(_load_pipx_listing(pipx))
    return UpdateResult(
        previous_version=before.version,
        current_version=after.version,
    )


def _load_pipx_listing(pipx: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [pipx, "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise UpdateError(f"could not inspect pipx installations: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise UpdateError(f"could not inspect pipx installations{suffix}")

    try:
        listing = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise UpdateError("pipx returned invalid installation metadata") from error
    if not isinstance(listing, dict):
        raise UpdateError("pipx returned invalid installation metadata")
    return listing


def _find_active_installation(listing: dict[str, Any]) -> _PipxInstallation:
    active_environment = Path(sys.prefix).resolve()
    environments = listing.get("venvs")
    if not isinstance(environments, dict):
        raise UpdateError("pipx returned invalid installation metadata")

    for environment in environments.values():
        if not isinstance(environment, dict):
            continue
        metadata = environment.get("metadata")
        if not isinstance(metadata, dict):
            continue
        package = metadata.get("main_package")
        if not isinstance(package, dict) or _normalize_name(package.get("package")) not in {
            PACKAGE_NAME,
            LEGACY_PACKAGE_NAME,
        }:
            continue

        app_paths = package.get("app_paths")
        if not isinstance(app_paths, list):
            continue
        for encoded_path in app_paths:
            app_path = _decode_path(encoded_path)
            if app_path is None or app_path.name not in EXECUTABLE_NAMES:
                continue
            if app_path.resolve().parent.parent != active_environment:
                continue

            version = package.get("package_version")
            if not isinstance(version, str) or not version:
                raise UpdateError("pipx did not report the installed version")
            return _PipxInstallation(version=version)

    raise UpdateError(
        "the running copy is not managed by pipx; update it with the package manager "
        "that installed it"
    )


def _decode_path(value: object) -> Path | None:
    if isinstance(value, str):
        return Path(value)
    if isinstance(value, dict):
        path = value.get("__Path__")
        if isinstance(path, str):
            return Path(path)
    return None


def _normalize_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.lower().replace("_", "-")
