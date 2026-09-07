"""Pseudo-terminal wrapper used to run an interactive child application."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import sys
import termios
import time
import tty
from collections.abc import Callable, Sequence
from contextlib import suppress

from sidequest.corrector import CorrectionEngine
from sidequest.input_processor import InputProcessor


class TerminalRequiredError(RuntimeError):
    """Raised when the proxy is invoked without an interactive terminal."""


def run_in_pty(
    command: Sequence[str],
    *,
    corrections: bool = True,
    corrector: CorrectionEngine | None = None,
    on_user_input: Callable[[bytes], None] | None = None,
    on_child_output: Callable[[bytes], None] | None = None,
) -> int:
    """Run *command* in a child PTY and proxy the current terminal to it."""
    if not command:
        raise ValueError("command cannot be empty")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise TerminalRequiredError("stdin and stdout must both be interactive terminals")

    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        try:
            os.execvp(command[0], list(command))
        except OSError as error:
            message = f"sidequest: unable to launch {command[0]}: {error}\n"
            with suppress(OSError):
                os.write(sys.stderr.fileno(), message.encode("utf-8", errors="replace"))
            os._exit(127)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    original_attributes = termios.tcgetattr(stdin_fd)
    old_winch_handler = signal.getsignal(signal.SIGWINCH)
    forwarded_signals = (signal.SIGHUP, signal.SIGTERM)
    old_signal_handlers = {signum: signal.getsignal(signum) for signum in forwarded_signals}
    processor = InputProcessor(corrector) if corrections else None

    def copy_window_size(*_: object) -> None:
        try:
            size = fcntl.ioctl(stdout_fd, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
            os.kill(child_pid, signal.SIGWINCH)
        except OSError:
            pass

    def forward_termination(signum: int, _frame: object) -> None:
        with suppress(ProcessLookupError):
            os.kill(child_pid, signum)
        raise SystemExit(128 + signum)

    try:
        tty.setraw(stdin_fd)
        signal.signal(signal.SIGWINCH, copy_window_size)
        for signum in forwarded_signals:
            signal.signal(signum, forward_termination)
        copy_window_size()

        while True:
            readable, _, _ = select.select([stdin_fd, master_fd], [], [])
            if master_fd in readable:
                try:
                    child_output = os.read(master_fd, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not child_output:
                    break
                if on_child_output is not None:
                    on_child_output(child_output)
                _write_all(stdout_fd, child_output)

            if stdin_fd in readable:
                user_input = os.read(stdin_fd, 4096)
                if not user_input:
                    _terminate_child(child_pid)
                    return 0
                if on_user_input is not None:
                    on_user_input(user_input)
                child_input = processor.feed(user_input) if processor else user_input
                try:
                    _write_all(master_fd, child_input)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
    except BaseException:
        _terminate_child(child_pid)
        raise
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, original_attributes)
        signal.signal(signal.SIGWINCH, old_winch_handler)
        for signum, handler in old_signal_handlers.items():
            signal.signal(signum, handler)
        with suppress(OSError):
            os.close(master_fd)

    _, status = os.waitpid(child_pid, 0)
    return _shell_exit_code(status)


def _terminate_child(child_pid: int) -> None:
    """Stop and reap the child when the proxy itself fails."""
    for signum, timeout in (
        (signal.SIGHUP, 0.5),
        (signal.SIGTERM, 0.5),
        (signal.SIGKILL, 0.5),
    ):
        with suppress(ProcessLookupError):
            os.kill(child_pid, signum)
        if _wait_for_child(child_pid, timeout):
            return


def _wait_for_child(child_pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            waited_pid, _ = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if waited_pid == child_pid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _shell_exit_code(wait_status: int) -> int:
    """Translate wait status to the conventional shell-compatible exit code."""
    exit_code = os.waitstatus_to_exitcode(wait_status)
    return 128 + abs(exit_code) if exit_code < 0 else exit_code


def _write_all(file_descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(file_descriptor, view)
        if written == 0:
            raise OSError(errno.EIO, "terminal write made no progress")
        view = view[written:]
