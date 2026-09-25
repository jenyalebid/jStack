import unittest

from demo.wordcount import count_words


class CountWords(unittest.TestCase):
    def test_case_is_folded_and_punctuation_separates(self):
        self.assertEqual(count_words("The cat and the hat."),
                         {"the": 2, "cat": 1, "and": 1, "hat": 1})

    def test_inner_apostrophes_stay_and_outer_ones_go(self):
        self.assertEqual(count_words("Don't stop, 'don't'!"), {"don't": 2, "stop": 1})

    def test_digits_are_words(self):
        self.assertEqual(count_words("route 66; Route 66"), {"route": 2, "66": 2})

    def test_nothing_counts_nothing(self):
        self.assertEqual(count_words(""), {})
        self.assertEqual(count_words(" -- ... "), {})


if __name__ == "__main__":
    unittest.main()
