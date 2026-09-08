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


if __name__ == "__main__":
    unittest.main()
