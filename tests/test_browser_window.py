import unittest
from unittest.mock import Mock, patch

from sidequest.browser_window import CompanionWindow
from sidequest.cdp import CDPError


def _mock_popen_process() -> Mock:
    process = Mock()
    process.poll.return_value = None
    return process


class CompanionWindowTests(unittest.TestCase):
    @patch("sidequest.browser_window.read_devtools_port", side_effect=CDPError("no chrome"))
    @patch("sidequest.browser_window.subprocess.Popen")
    @patch("sidequest.browser_window._find_chromium", return_value="/browser")
    def test_owned_browser_process_is_terminated(self, _find, popen, _port) -> None:
        process = _mock_popen_process()
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

    @patch("sidequest.browser_window.read_devtools_port", side_effect=CDPError("no chrome"))
    @patch("sidequest.browser_window.subprocess.Popen")
    @patch("sidequest.browser_window._find_chromium", return_value="/browser")
    def test_open_passes_the_requested_size_and_allows_autoplay(self, _find, popen, _port) -> None:
        process = _mock_popen_process()
        popen.return_value = process
        window = CompanionWindow(width=1000, height=600)
        try:
            window.open("http://127.0.0.1:1234/")
        finally:
            window.close()

        args = popen.call_args.args[0]
        self.assertIn("--window-size=1000,600", args)
        self.assertIn("--autoplay-policy=no-user-gesture-required", args)
        self.assertIn("--remote-debugging-port=0", args)

    @patch("sidequest.browser_window.read_devtools_port", side_effect=CDPError("no chrome"))
    @patch("sidequest.browser_window.subprocess.Popen")
    @patch("sidequest.browser_window._find_chromium", return_value="/browser")
    def test_navigate_without_cdp_falls_back_to_respawning(self, _find, popen, _port) -> None:
        # No CDP connection came up (the common real-world case if Chrome's
        # DevTools port is ever unreachable) -- navigate() should degrade to
        # the original close-then-reopen behavior rather than doing nothing.
        first_process = _mock_popen_process()
        second_process = _mock_popen_process()
        popen.side_effect = [first_process, second_process]
        window = CompanionWindow()
        try:
            window.open("http://127.0.0.1:1234/")
            window.navigate("http://127.0.0.1:1234/?mode=chess", 768, 650)
        finally:
            window.close()

        self.assertEqual(popen.call_count, 2)
        first_process.terminate.assert_called_once_with()
        second_args = popen.call_args_list[1].args[0]
        self.assertIn("--window-size=768,650", second_args)

    @patch("sidequest.browser_window.CDPConnection")
    @patch(
        "sidequest.browser_window.read_devtools_port",
        return_value=(9333, "/devtools/browser/abc"),
    )
    @patch("sidequest.browser_window.subprocess.Popen")
    @patch("sidequest.browser_window._find_chromium", return_value="/browser")
    def test_navigate_with_cdp_resizes_and_reloads_in_place(
        self, _find, popen, _port, connection_type
    ) -> None:
        popen.return_value = _mock_popen_process()
        connection = connection_type.return_value
        connection.attach_page.return_value = ("session-1", "target-1")
        connection.send.return_value = {"windowId": 7}
        window = CompanionWindow()
        try:
            window.open("http://127.0.0.1:1234/")
            window.navigate("http://127.0.0.1:1234/?mode=video", 1000, 650)
        finally:
            window.close()

        popen.assert_called_once()  # same process throughout -- no respawn
        calls = {call.args[0] for call in connection.send.call_args_list}
        self.assertIn("Browser.getWindowForTarget", calls)
        self.assertIn("Browser.setWindowBounds", calls)
        window_call = next(
            c for c in connection.send.call_args_list if c.args[0] == "Browser.getWindowForTarget"
        )
        self.assertEqual(window_call.args[1], {"targetId": "target-1"})
        bounds_call = next(
            c for c in connection.send.call_args_list if c.args[0] == "Browser.setWindowBounds"
        )
        self.assertEqual(bounds_call.args[1]["bounds"], {"width": 1000, "height": 650})
        navigate_call = next(
            c for c in connection.send.call_args_list if c.args[0] == "Page.navigate"
        )
        self.assertEqual(navigate_call.args[1], {"url": "http://127.0.0.1:1234/?mode=video"})
        self.assertEqual(navigate_call.kwargs, {"session_id": "session-1"})
        connection.close.assert_called_once_with()  # torn down on window.close()


if __name__ == "__main__":
    unittest.main()
