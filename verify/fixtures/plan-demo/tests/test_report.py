import unittest

from demo.report import top


class Top(unittest.TestCase):
    def test_highest_count_first(self):
        self.assertEqual(top("b a b c a b", 2), [("b", 3), ("a", 2)])

    def test_ties_break_alphabetically(self):
        self.assertEqual(top("z y x", 2), [("x", 1), ("y", 1)])

    def test_words_are_stage_ones_words(self):
        # Only a real count_words folds these three into one word.
        self.assertEqual(top("The the, THE! cat.", 1), [("the", 3)])

    def test_zero_is_empty(self):
        self.assertEqual(top("a b", 0), [])


if __name__ == "__main__":
    unittest.main()
