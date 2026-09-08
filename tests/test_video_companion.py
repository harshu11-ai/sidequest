import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from sidequest.video.companion import VideoCompanion
from sidequest.video.queue import VideoQueue


def _entry(letter: str, duration_s: int) -> dict[str, object]:
    return {
        "id": letter,
        "title": f"Video {letter.upper()}",
        "channel": f"Chan {letter.upper()}",
        "youtube_id": letter * 11,
        "duration_s": duration_s,
    }


_CATALOG = [_entry("a", 100), _entry("b", 200)]


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


class VideoCompanionTests(unittest.TestCase):
    def setUp(self) -> None:
        # A no-op shuffle keeps the draw order deterministic (catalog order)
        # so tests can assert on specific video ids instead of "some id".
        self.shuffle_patcher = patch("sidequest.video.queue.random.shuffle")
        self.shuffle_patcher.start()
        self.temporary_directory = tempfile.TemporaryDirectory()
        queue = VideoQueue(
            Path(self.temporary_directory.name) / "video.json",
            catalog=_CATALOG,
        )
        self.opened_urls: list[str] = []
        self.close_count = 0
        self.companion = VideoCompanion(
            queue,
            browser_open=self.opened_urls.append,
            browser_close=self._record_close,
        )

    def tearDown(self) -> None:
        self.companion.close()
        self.temporary_directory.cleanup()
        self.shuffle_patcher.stop()

    def _record_close(self) -> None:
        self.close_count += 1

    def _url(self, path: str) -> str:
        return self.companion.play_url.replace("/?", f"{path}?")

    def test_show_is_idempotent_until_hidden(self) -> None:
        self.companion.show()
        self.companion.show()
        self.assertEqual(self.opened_urls, [self.companion.play_url])
        self.companion.hide()
        self.assertEqual(self.close_count, 1)
        self.companion.show()
        self.assertEqual(len(self.opened_urls), 2)

    def test_state_endpoint_requires_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.companion.base_url}/api/state", timeout=2)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_state_endpoint_returns_current_video_and_trimmed_upcoming(self) -> None:
        with urllib.request.urlopen(self._url("/api/state"), timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["video"]["id"], "a")
        self.assertFalse(payload["active"])
        self.assertEqual([entry["id"] for entry in payload["upcoming"]], ["b"])
        self.assertEqual(set(payload["upcoming"][0]), {"id", "title", "channel", "duration_s"})

    def test_position_endpoint_updates_queue(self) -> None:
        payload = _post(self._url("/api/position"), {"position_s": 12.5})
        self.assertEqual(payload["position_s"], 12.5)

    def test_position_endpoint_rejects_bad_value(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            _post(self._url("/api/position"), {"position_s": "nope"})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_skip_endpoint_advances_queue(self) -> None:
        payload = _post(self._url("/api/skip"), {})
        self.assertEqual(payload["video"]["id"], "b")
        self.assertEqual(payload["watched"], ["a"])

    def test_lifecycle_endpoint_changes_visibility(self) -> None:
        start = urllib.request.Request(
            self.companion.lifecycle_url("start"), data=b"{}", method="POST"
        )
        stop = urllib.request.Request(
            self.companion.lifecycle_url("stop"), data=b"{}", method="POST"
        )
        urllib.request.urlopen(start, timeout=2).close()
        self.assertTrue(self.companion.is_active())
        urllib.request.urlopen(stop, timeout=2).close()
        self.assertFalse(self.companion.is_active())

    def test_serves_bundled_page_with_security_headers(self) -> None:
        with urllib.request.urlopen(self.companion.base_url + "/", timeout=2) as response:
            self.assertEqual(response.headers.get_content_type(), "text/html")
            csp = response.headers.get("Content-Security-Policy")
            self.assertIn("https://www.youtube-nocookie.com", csp)
            self.assertTrue(response.read(20))


if __name__ == "__main__":
    unittest.main()
