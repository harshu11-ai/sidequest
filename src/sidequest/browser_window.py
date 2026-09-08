"""A disposable app-style browser window shared by sidequest's companions."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import webbrowser
from contextlib import suppress

_MACOS_CHROMIUM_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


class CompanionWindow:
    """Launch a disposable app-style browser window that can be closed reliably."""

    def __init__(self, *, width: int = 768, height: int = 650) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._profile = tempfile.TemporaryDirectory(prefix="sidequest-browser-")
        self._width = width
        self._height = height

    def open(self, url: str) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        executable = _find_chromium()
        if executable is None:
            webbrowser.open_new(url)
            return
        try:
            self._process = subprocess.Popen(
                [
                    executable,
                    f"--app={url}",
                    f"--user-data-dir={self._profile.name}",
                    f"--window-size={self._width},{self._height}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    # Each window gets a brand-new temp profile (see
                    # __init__), so Chrome's normal per-site autoplay
                    # heuristic (which needs a history of engagement) never
                    # has anything to go on. This is a single-purpose,
                    # sidequest-controlled window, not general browsing, so
                    # it's safe to just always allow autoplay here -- lets
                    # the video companion's own playVideo() call actually
                    # start playback (with sound) the moment it opens.
                    "--autoplay-policy=no-user-gesture-required",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            webbrowser.open_new(url)

    def hide(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        with suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)

    def close(self) -> None:
        self.hide()
        self._profile.cleanup()


def _find_chromium() -> str | None:
    for path in _MACOS_CHROMIUM_PATHS:
        if os.access(path, os.X_OK):
            return path
    for name in ("google-chrome", "chromium", "chromium-browser", "brave-browser", "msedge"):
        executable = shutil.which(name)
        if executable:
            return executable
    return None
