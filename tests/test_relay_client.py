import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from sidequest.relay_client import RelayClient, RelayError


def _http_error(status: int, body: dict) -> urllib.error.HTTPError:
    error = urllib.error.HTTPError(
        url="http://relay/x", code=status, msg="error", hdrs=None, fp=None
    )
    error.read = Mock(return_value=json.dumps(body).encode("utf-8"))
    return error


class RelayClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = RelayClient("http://relay.example")

    @patch("sidequest.relay_client.urllib.request.urlopen")
    def test_create_room_posts_and_parses_seat(self, urlopen) -> None:
        response = Mock()
        response.read.return_value = json.dumps(
            {"code": "ABC123", "token": "tok", "you": "white"}
        ).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        urlopen.return_value = response

        seat = self.client.create_room()

        self.assertEqual(seat.code, "ABC123")
        self.assertEqual(seat.you, "white")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "http://relay.example/rooms")

    @patch("sidequest.relay_client.urllib.request.urlopen")
    def test_get_state_encodes_token_as_query_param(self, urlopen) -> None:
        response = Mock()
        response.read.return_value = json.dumps({"fen": "startpos"}).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        urlopen.return_value = response

        self.client.get_state("ABC123", "my token")

        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("token=my", request.full_url)

    @patch("sidequest.relay_client.urllib.request.urlopen")
    def test_submit_move_sends_json_body(self, urlopen) -> None:
        response = Mock()
        response.read.return_value = json.dumps({"turn": "black"}).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        urlopen.return_value = response

        self.client.submit_move("ABC123", "tok", "e2e4", 0)

        request = urlopen.call_args[0][0]
        body = json.loads(request.data)
        self.assertEqual(body, {"token": "tok", "move": "e2e4", "expected_move_count": 0})
        self.assertEqual(request.headers.get("Content-type"), "application/json")

    @patch("sidequest.relay_client.urllib.request.urlopen")
    def test_http_error_becomes_relay_error_with_detail(self, urlopen) -> None:
        urlopen.side_effect = _http_error(409, {"detail": "it is not your turn"})

        with self.assertRaisesRegex(RelayError, "it is not your turn"):
            self.client.submit_move("ABC123", "tok", "e2e4", 0)

    @patch("sidequest.relay_client.urllib.request.urlopen")
    def test_network_error_becomes_relay_error(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.URLError("connection refused")

        with self.assertRaisesRegex(RelayError, "could not reach the relay"):
            self.client.create_room()


if __name__ == "__main__":
    unittest.main()
