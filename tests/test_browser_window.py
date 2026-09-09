import unittest
from unittest.mock import Mock, patch

from sidequest.browser_window import CompanionWindow


class CompanionWindowTests(unittest.TestCase):
    @patch("sidequest.browser_window.subprocess.Popen")
    @patch("sidequest.browser_window._find_chromium", return_value="/browser")
    def test_owned_browser_process_is_terminated(self, _find, popen) -> None:
        process = Mock()
        process.poll.return_value = None
        popen.return_value = process
        window = CompanionWindow()
        try:
            window.open("http://127.0.0.1:1234/")
            window.hide()
        finally:
            window.close()

        popen.assert_called_once()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)

    @patch("sidequest.browser_window.subprocess.Popen")
    @patch("sidequest.browser_window._find_chromium", return_value="/browser")
    def test_open_passes_the_requested_size_and_allows_autoplay(self, _find, popen) -> None:
        process = Mock()
        process.poll.return_value = None
        popen.return_value = process
        window = CompanionWindow(width=1000, height=600)
        try:
            window.open("http://127.0.0.1:1234/")
        finally:
            window.close()

        args = popen.call_args.args[0]
        self.assertIn("--window-size=1000,600", args)
        self.assertIn("--autoplay-policy=no-user-gesture-required", args)

    @patch("sidequest.browser_window.subprocess.Popen")
    @patch("sidequest.browser_window._find_chromium", return_value="/browser")
    def test_is_running_reflects_the_live_process(self, _find, popen) -> None:
        process = Mock()
        process.poll.return_value = None
        popen.return_value = process
        window = CompanionWindow()
        try:
            self.assertFalse(window.is_running())  # never opened
            window.open("http://127.0.0.1:1234/")
            self.assertTrue(window.is_running())
        finally:
            window.close()
        self.assertFalse(window.is_running())  # closed

    @patch("sidequest.browser_window.subprocess.Popen")
    @patch("sidequest.browser_window._find_chromium", return_value="/browser")
    def test_is_running_is_false_once_the_user_closes_the_window_themselves(
        self, _find, popen
    ) -> None:
        process = Mock()
        process.poll.return_value = None
        popen.return_value = process
        window = CompanionWindow()
        try:
            window.open("http://127.0.0.1:1234/")
            self.assertTrue(window.is_running())

            # Nothing in sidequest called hide() -- the OS process just
            # exited on its own, the way it would if the user closed the
            # window themselves.
            process.poll.return_value = 0
            self.assertFalse(window.is_running())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
