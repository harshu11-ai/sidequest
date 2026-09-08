import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequest.video.queue import VideoQueue


def _entry(letter: str, duration_s: int) -> dict[str, object]:
    return {
        "id": letter,
        "title": f"Video {letter.upper()}",
        "channel": f"Chan {letter.upper()}",
        "youtube_id": letter * 11,
        "duration_s": duration_s,
    }


_CATALOG = [_entry("a", 100), _entry("b", 200), _entry("c", 300)]
_IDS = {entry["id"] for entry in _CATALOG}


class VideoQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "video.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _queue(self) -> VideoQueue:
        return VideoQueue(self.state_path, catalog=_CATALOG)

    def test_starts_on_some_catalog_entry(self) -> None:
        snapshot = self._queue().snapshot()
        self.assertIn(snapshot.video["id"], _IDS)
        self.assertEqual(snapshot.position_s, 0.0)
        self.assertEqual(snapshot.watched, ())

    def test_upcoming_excludes_the_current_video_and_caps_at_catalog_size(self) -> None:
        snapshot = self._queue().snapshot()
        self.assertNotIn(snapshot.video["id"], snapshot.upcoming)
        self.assertLessEqual(len(snapshot.upcoming), len(_CATALOG) - 1)

    def test_records_position_for_current_video(self) -> None:
        queue = self._queue()
        snapshot = queue.record_position(42.5)
        self.assertEqual(snapshot.position_s, 42.5)

    def test_negative_position_is_clamped_to_zero(self) -> None:
        snapshot = self._queue().record_position(-5)
        self.assertEqual(snapshot.position_s, 0.0)

    def test_rejects_unparsable_position(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid position"):
            self._queue().record_position("not a number")

    def test_skip_moves_to_a_different_video_and_marks_watched(self) -> None:
        queue = self._queue()
        first_id = queue.snapshot().video["id"]
        queue.record_position(50)

        snapshot = queue.skip()

        self.assertIn(snapshot.video["id"], _IDS)
        self.assertNotEqual(snapshot.video["id"], first_id)
        self.assertEqual(snapshot.position_s, 0.0)
        self.assertEqual(snapshot.watched, (first_id,))

    @patch("sidequest.video.queue.random.shuffle")
    def test_repeated_skips_eventually_touch_every_video(self, _shuffle) -> None:
        # Shuffle is a no-op here (identity order) so this stays deterministic
        # instead of relying on randomness to happen to cover every video.
        queue = self._queue()
        seen = {queue.snapshot().video["id"]}
        for _ in range(len(_CATALOG) * 3):
            seen.add(queue.skip().video["id"])
        self.assertEqual(seen, _IDS)

    def test_persists_and_restores_position(self) -> None:
        first = self._queue()
        first.skip()
        current_id = first.snapshot().video["id"]
        first.record_position(17)

        restored = VideoQueue(self.state_path, catalog=_CATALOG).snapshot()

        self.assertEqual(restored.video["id"], current_id)
        self.assertEqual(restored.position_s, 17.0)
        self.assertGreaterEqual(len(restored.watched), 1)

    def test_invalid_saved_state_starts_over(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"current_id": 123}', encoding="utf-8")
        snapshot = VideoQueue(self.state_path, catalog=_CATALOG).snapshot()
        self.assertIn(snapshot.video["id"], _IDS)

    def test_unknown_saved_current_id_falls_back_to_a_fresh_draw(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"current_id": "not-in-catalog"}', encoding="utf-8")
        snapshot = VideoQueue(self.state_path, catalog=_CATALOG).snapshot()
        self.assertIn(snapshot.video["id"], _IDS)

    def test_single_video_catalog_does_not_crash_on_skip(self) -> None:
        queue = VideoQueue(self.state_path, catalog=[_entry("a", 100)])
        snapshot = queue.skip()
        self.assertEqual(snapshot.video["id"], "a")
        self.assertEqual(snapshot.upcoming, ())

    def test_previous_is_a_noop_with_no_history(self) -> None:
        queue = self._queue()
        before = queue.snapshot()
        self.assertFalse(before.can_go_back)

        after = queue.previous()

        self.assertEqual(after.video["id"], before.video["id"])
        self.assertFalse(after.can_go_back)

    def test_skip_then_previous_returns_to_the_prior_video(self) -> None:
        queue = self._queue()
        first_id = queue.snapshot().video["id"]
        queue.record_position(30)
        skipped = queue.skip()
        self.assertTrue(skipped.can_go_back)

        back = queue.previous()

        self.assertEqual(back.video["id"], first_id)
        self.assertEqual(back.position_s, 0.0)

    def test_previous_does_not_mark_the_left_video_watched(self) -> None:
        queue = self._queue()
        queue.skip()
        second_id = queue.snapshot().video["id"]

        back = queue.previous()

        self.assertNotIn(second_id, back.watched)

    def test_previous_persists_across_reload(self) -> None:
        first = self._queue()
        first.skip()
        id_before_second_skip = first.snapshot().video["id"]
        first.skip()

        restored = VideoQueue(self.state_path, catalog=_CATALOG)
        back = restored.previous()

        self.assertEqual(back.video["id"], id_before_second_skip)


if __name__ == "__main__":
    unittest.main()
