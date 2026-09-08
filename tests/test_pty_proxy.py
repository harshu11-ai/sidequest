import errno
import os
import pty
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequest.pty_proxy import _SUBMIT_SPLIT_DELAY_S, _shell_exit_code, _write_all

ROOT = Path(__file__).resolve().parents[1]


class PtyProxyIntegrationTests(unittest.TestCase):
    def test_translates_normal_and_signal_exit_codes(self) -> None:
        self.assertEqual(_shell_exit_code(7 << 8), 7)
        self.assertEqual(_shell_exit_code(15), 143)

    @patch("sidequest.pty_proxy.os.write", side_effect=[2, 3])
    def test_write_all_retries_partial_writes(self, write) -> None:
        _write_all(9, b"hello")
        self.assertEqual(write.call_count, 2)

    @patch("sidequest.pty_proxy.os.write", return_value=0)
    def test_write_all_rejects_zero_progress(self, _write) -> None:
        with self.assertRaisesRegex(OSError, "made no progress"):
            _write_all(9, b"hello")

    def test_child_receives_rewrite_sequence(self) -> None:
        child_code = """
import os
import tty

tty.setraw(0)
os.write(1, b"CHILD_READY\\n")
data = b""
while len(data) < 10:
    data += os.read(0, 10 - len(data))
os.write(1, b"RECEIVED:" + data.hex().encode("ascii") + b"\\n")
"""
        driver_code = f"""
import sys
from sidequest.pty_proxy import run_in_pty

raise SystemExit(run_in_pty([sys.executable, "-c", {child_code!r}]))
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-c", driver_code],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=environment,
            close_fds=True,
        )
        os.close(slave_fd)

        transcript = bytearray()
        expected = b"RECEIVED:7465687f7f7f74686520"
        try:
            transcript.extend(self._read_until(master_fd, b"CHILD_READY", process))
            os.write(master_fd, b"teh ")
            transcript.extend(self._read_until(master_fd, expected, process))
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master_fd)

        # "teh" is forwarded as typed, followed by three DEL bytes, "the",
        # and the boundary space.
        self.assertIn(expected, transcript)

    def test_correction_before_enter_is_split_into_two_writes(self) -> None:
        # A correction that lands right on Enter must reach the child as two
        # separate reads with a real gap between them -- sent as one
        # uninterrupted burst, some agent TUIs mistake it for a paste and
        # insert a newline instead of submitting. See
        # InputProcessor.pending_submit_split.
        child_code = """
import os
import time
import tty

tty.setraw(0)
os.write(1, b"CHILD_READY\\n")
chunks = []
times = []
total = 0
while total < 10:
    chunk = os.read(0, 10 - total)
    chunks.append(chunk)
    times.append(time.monotonic())
    total += len(chunk)
data = b"".join(chunks)
gap = max((times[i] - times[i - 1] for i in range(1, len(times))), default=0.0)
report = b"RECEIVED:" + data.hex().encode("ascii")
report += b":CHUNKS:" + str(len(chunks)).encode("ascii")
report += b":GAP:" + repr(gap).encode("ascii")
os.write(1, report + b"\\n")
"""
        driver_code = f"""
import sys
from sidequest.pty_proxy import run_in_pty

raise SystemExit(run_in_pty([sys.executable, "-c", {child_code!r}]))
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-c", driver_code],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=environment,
            close_fds=True,
        )
        os.close(slave_fd)

        # "teh" is echoed live as typed, then erased and replaced with "the"
        # on a real carriage return (raw-mode Enter) rather than the space
        # used elsewhere in this file -- CR is the boundary this fix splits.
        expected_hex = b"746568" + b"7f7f7f" + b"746865" + b"0d"  # "teh", DEL*3, "the", CR
        transcript = bytearray()
        try:
            transcript.extend(self._read_until(master_fd, b"CHILD_READY", process))
            os.write(master_fd, b"teh\r")
            transcript.extend(self._read_until(master_fd, b"GAP:", process, timeout=5))
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master_fd)

        line = bytes(transcript).splitlines()[-1]
        self.assertIn(expected_hex, line)
        chunk_count = int(line.split(b":CHUNKS:")[1].split(b":GAP:")[0])
        gap = float(line.split(b":GAP:")[1])
        self.assertGreaterEqual(chunk_count, 2, f"expected a split write, got one chunk: {line!r}")
        self.assertGreaterEqual(gap, _SUBMIT_SPLIT_DELAY_S * 0.5)

    @staticmethod
    def _read_until(
        master_fd: int,
        marker: bytes,
        process: subprocess.Popen[bytes],
        timeout: float = 5,
    ) -> bytes:
        deadline = time.monotonic() + timeout
        collected = bytearray()
        while marker not in collected and time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if not readable:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            collected.extend(chunk)

        if marker not in collected:
            raise AssertionError(
                f"timed out waiting for {marker!r}; received {bytes(collected)!r}"
            )
        return bytes(collected)


if __name__ == "__main__":
    unittest.main()
