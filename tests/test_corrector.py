import unittest

from sidequest.autocorrect.corrector import ConservativeCorrector


class ConservativeCorrectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corrector = ConservativeCorrector()

    def test_corrects_curated_typo(self) -> None:
        correction = self.corrector.suggest("teh")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "the")
        self.assertEqual(correction.reason, "common typo")

    def test_preserves_trailing_punctuation(self) -> None:
        correction = self.corrector.suggest("fucntion,")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "function,")

    def test_finds_unique_adjacent_transposition(self) -> None:
        correction = self.corrector.suggest("waht")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "what")
        self.assertEqual(correction.reason, "adjacent transposition")

    def test_corrects_liek_immediately_without_dictionary(self) -> None:
        correction = self.corrector.suggest("liek")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "like")
        self.assertEqual(correction.reason, "adjacent transposition")

    def test_removes_one_unambiguous_extra_character(self) -> None:
        correction = self.corrector.suggest("gfix")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "fix")
        self.assertEqual(correction.reason, "single extra character")

    def test_expands_personal_abbreviation_with_punctuation(self) -> None:
        corrector = ConservativeCorrector(abbreviations={"pr": "pull request (PR)"})
        correction = corrector.suggest("pr,")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "pull request (PR),")
        self.assertEqual(correction.reason, "personal abbreviation")

    def test_personal_abbreviation_takes_priority_over_spelling(self) -> None:
        corrector = ConservativeCorrector(
            custom_corrections={"teh": "the"},
            abbreviations={"teh": "technical explanation here"},
        )
        correction = corrector.suggest("teh")
        self.assertIsNotNone(correction)
        assert correction is not None
        self.assertEqual(correction.replacement, "technical explanation here")

    def test_abstains_from_ambiguous_typo(self) -> None:
        self.assertIsNone(self.corrector.suggest("wnat"))

    def test_abstains_from_unknown_word(self) -> None:
        self.assertIsNone(self.corrector.suggest("candidatee"))

    def test_protects_technical_tokens(self) -> None:
        protected = [
            "src/teh.py",
            "--teh",
            "teh_function",
            "teh-function",
            "useEffect",
            "BMPR2",
            "GPT5",
            "user@example.com",
            "https://example.com/teh",
        ]
        for token in protected:
            with self.subTest(token=token):
                self.assertIsNone(self.corrector.suggest(token))


if __name__ == "__main__":
    unittest.main()
