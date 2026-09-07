import unittest

from sidequest.autocorrect.corrector import FrequencyCorrector


class FrequencyCorrectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corrector = FrequencyCorrector(background=False)
        if cls.corrector.load_error is not None:
            raise cls.corrector.load_error

    def test_corrects_substitution(self) -> None:
        correction = self.corrector.suggest("suvk")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "suck")

    def test_corrects_high_confidence_two_edit_typo(self) -> None:
        correction = self.corrector.suggest("typicing")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "typing")

    def test_corrects_screenshot_phrase(self) -> None:
        self.assertEqual(self.corrector.suggest("mes").replacement, "me")

    def test_corrects_top_ranked_transposition(self) -> None:
        correction = self.corrector.suggest("liek")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "like")

    def test_abstains_when_nearest_candidates_are_ambiguous(self) -> None:
        self.assertIsNone(self.corrector.suggest("wnat"))
        self.assertIsNone(self.corrector.suggest("candidatee"))

    def test_abstains_from_likely_technical_vocabulary(self) -> None:
        for word in ("pydantic", "pytest", "numpy"):
            with self.subTest(word=word):
                self.assertIsNone(self.corrector.suggest(word))


if __name__ == "__main__":
    unittest.main()
