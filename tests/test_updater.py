import json
import subprocess
import unittest
from unittest.mock import call, patch

from sidequest.updater import UPDATE_SOURCE, UpdateError, update_with_pipx

PIPX = "/usr/local/bin/pipx"
ACTIVE_ENVIRONMENT = "/example/pipx/venvs/sidequest"
APP_PATH = f"{ACTIVE_ENVIRONMENT}/bin/cauto"


def pipx_listing(version: str = "0.2.1", app_path: object = APP_PATH) -> str:
    return json.dumps(
        {
            "venvs": {
                "sidequest": {
                    "metadata": {
                        "main_package": {
                            "app_paths": [{"__Path__": app_path}],
                            "package": "sidequest",
                            "package_version": version,
                        }
                    }
                }
            }
        }
    )


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class UpdaterTests(unittest.TestCase):
    @patch("sidequest.updater.sys.prefix", ACTIVE_ENVIRONMENT)
    @patch("sidequest.updater.shutil.which", return_value=PIPX)
    @patch("sidequest.updater.subprocess.run")
    def test_force_reinstalls_active_pipx_environment(self, run, _which) -> None:
        run.side_effect = [
            completed(stdout=pipx_listing("0.2.1")),
            completed(),
            completed(stdout=pipx_listing("0.2.2")),
        ]

        result = update_with_pipx()

        self.assertEqual(result.previous_version, "0.2.1")
        self.assertEqual(result.current_version, "0.2.2")
        self.assertEqual(
            run.call_args_list,
            [
                call([PIPX, "list", "--json"], check=False, capture_output=True, text=True),
                call([PIPX, "install", "--force", UPDATE_SOURCE], check=False),
                call([PIPX, "list", "--json"], check=False, capture_output=True, text=True),
            ],
        )

    @patch("sidequest.updater.shutil.which", return_value=None)
    def test_requires_pipx(self, _which) -> None:
        with self.assertRaisesRegex(UpdateError, "pipx is not available"):
            update_with_pipx()

    @patch("sidequest.updater.sys.prefix", "/example/project/.venv")
    @patch("sidequest.updater.shutil.which", return_value=PIPX)
    @patch("sidequest.updater.subprocess.run")
    def test_refuses_non_pipx_environment(self, run, _which) -> None:
        run.return_value = completed(stdout=pipx_listing())

        with self.assertRaisesRegex(UpdateError, "running copy is not managed by pipx"):
            update_with_pipx()

        run.assert_called_once_with(
            [PIPX, "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )

    @patch("sidequest.updater.sys.prefix", ACTIVE_ENVIRONMENT)
    @patch("sidequest.updater.shutil.which", return_value=PIPX)
    @patch("sidequest.updater.subprocess.run")
    def test_reports_failed_reinstall(self, run, _which) -> None:
        run.side_effect = [
            completed(stdout=pipx_listing()),
            completed(returncode=9),
        ]

        with self.assertRaisesRegex(UpdateError, "pipx exited with status 9"):
            update_with_pipx()

    @patch("sidequest.updater.shutil.which", return_value=PIPX)
    @patch("sidequest.updater.subprocess.run")
    def test_rejects_invalid_pipx_metadata(self, run, _which) -> None:
        run.return_value = completed(stdout="not json")

        with self.assertRaisesRegex(UpdateError, "invalid installation metadata"):
            update_with_pipx()


if __name__ == "__main__":
    unittest.main()
