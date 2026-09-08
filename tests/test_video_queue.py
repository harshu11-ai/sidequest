import tempfile
import unittest
from pathlib import Path

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


class VideoQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "video.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _queue(self) -> VideoQueue:
        return VideoQueue(self.state_path, catalog=_CATALOG)

    def test_starts_at_the_first_catalog_entry(self) -> None:
        snapshot = self._queue().snapshot()
        self.assertEqual(snapshot.video["id"], "a")
        self.assertEqual(snapshot.index, 0)
        self.assertEqual(snapshot.count, 3)
        self.assertEqual(snapshot.position_s, 0.0)
        self.assertEqual(snapshot.watched, ())

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

    def test_skip_advances_and_marks_watched(self) -> None:
        queue = self._queue()
        queue.record_position(50)
        snapshot = queue.skip()
        self.assertEqual(snapshot.video["id"], "b")
        self.assertEqual(snapshot.position_s, 0.0)
        self.assertEqual(snapshot.watched, ("a",))

    def test_skip_wraps_around_the_catalog(self) -> None:
        queue = self._queue()
        queue.skip()
        queue.skip()
        snapshot = queue.skip()
        self.assertEqual(snapshot.video["id"], "a")
        self.assertEqual(snapshot.watched, ("a", "b", "c"))

    def test_previous_wraps_backward(self) -> None:
        snapshot = self._queue().previous()
        self.assertEqual(snapshot.video["id"], "c")

    def test_persists_and_restores_position(self) -> None:
        first = self._queue()
        first.skip()
        first.record_position(17)

        restored = VideoQueue(self.state_path, catalog=_CATALOG).snapshot()

        self.assertEqual(restored.video["id"], "b")
        self.assertEqual(restored.position_s, 17.0)
        self.assertEqual(restored.watched, ("a",))

    def test_invalid_saved_state_starts_over(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"index": "nonsense"}', encoding="utf-8")
        snapshot = VideoQueue(self.state_path, catalog=_CATALOG).snapshot()
        self.assertEqual(snapshot.video["id"], "a")

    def test_out_of_range_saved_index_falls_back_to_start(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"index": 99}', encoding="utf-8")
        snapshot = VideoQueue(self.state_path, catalog=_CATALOG).snapshot()
        self.assertEqual(snapshot.index, 0)


if __name__ == "__main__":
    unittest.main()
