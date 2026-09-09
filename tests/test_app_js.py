"""Runs the chess board's client-side state machine (the last-turn grace
period and the finished-window handoff) against the real app.js source, via
a small stubbed-DOM harness executed in Node. See tests/js_harness for
details.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HARNESS = Path(__file__).resolve().parent / "js_harness" / "verify_app_js.js"
_APP_JS = _REPO_ROOT / "src" / "sidequest" / "chess" / "web" / "app.js"


@unittest.skipIf(shutil.which("node") is None, "node is not available on PATH")
class AppJsStateMachineTests(unittest.TestCase):
    def test_state_machine_harness_passes(self) -> None:
        result = subprocess.run(
            ["node", str(_HARNESS), str(_APP_JS)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"app.js state-machine harness failed:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("All last-turn state-machine assertions passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
