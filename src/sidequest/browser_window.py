"""A disposable app-style browser window shared by sidequest's companions."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import webbrowser
from contextlib import suppress
from pathlib import Path

from sidequest.cdp import CDPConnection, CDPError, read_devtools_port

_MACOS_CHROMIUM_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


class CompanionWindow:
    """Launch a disposable app-style browser window that can be closed reliably.

    Where possible, `navigate()` resizes and reloads the *same* window in
    place via Chrome's own DevTools Protocol (no OS permission needed --
    Chrome managing its own window, not a different app's). If that never
    comes up (CDP unreachable, handshake fails, ...), it falls back to the
    close-then-reopen behavior `open()`/`hide()` always had -- degraded to
    "not persistent" rather than broken.
    """

    def __init__(self, *, width: int = 768, height: int = 650) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._profile = tempfile.TemporaryDirectory(prefix="sidequest-browser-")
        self._width = width
        self._height = height
        self._cdp: CDPConnection | None = None
        self._cdp_session_id: str | None = None
        self._cdp_target_id: str | None = None

    def open(self, url: str) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        executable = _find_chromium()
        if executable is None:
            webbrowser.open_new(url)
            return
        # navigate()'s close-then-reopen fallback reuses this same profile
        # directory for a fresh process; without this, read_devtools_port()
        # below could read the *previous* process's now-dead port straight
        # off, since the file already exists and looks valid before Chrome
        # gets a chance to overwrite it with the new one.
        with suppress(OSError):
            (Path(self._profile.name) / "DevToolsActivePort").unlink()
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
                    # Only used by navigate() below, to resize/reload this
                    # same window in place instead of respawning it. Local
                    # loopback only, torn down with the rest of the process.
                    "--remote-debugging-port=0",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            webbrowser.open_new(url)
            return
        self._connect_cdp()

    def _connect_cdp(self) -> None:
        try:
            port, path = read_devtools_port(Path(self._profile.name), timeout=5.0)
            connection = CDPConnection("127.0.0.1", port, path, timeout=3.0)
            session_id, target_id = connection.attach_page()
        except (CDPError, OSError, ValueError):
            self._cdp = None
            self._cdp_session_id = None
            self._cdp_target_id = None
            return
        self._cdp = connection
        self._cdp_session_id = session_id
        self._cdp_target_id = target_id

    def navigate(self, url: str, width: int, height: int) -> None:
        """Resize and reload this window in place, if it's already open.

        Falls back to close-then-reopen (today's `hide()` + `open()`) when
        no working CDP connection is available.
        """
        if self._cdp is not None:
            try:
                self._cdp.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": self._cdp.send(
                            "Browser.getWindowForTarget", {"targetId": self._cdp_target_id}
                        )["windowId"],
                        "bounds": {"width": width, "height": height},
                    },
                )
                self._cdp.send("Page.navigate", {"url": url}, session_id=self._cdp_session_id)
                return
            except (CDPError, OSError):
                # The connection died under us -- drop it and fall through
                # to the respawn path below, same as never having had one.
                self._cdp.close()
                self._cdp = None
                self._cdp_session_id = None
                self._cdp_target_id = None
        self._width, self._height = width, height
        self.hide()
        self.open(url)

    def hide(self) -> None:
        process = self._process
        self._process = None
        if self._cdp is not None:
            self._cdp.close()
            self._cdp = None
            self._cdp_session_id = None
            self._cdp_target_id = None
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
