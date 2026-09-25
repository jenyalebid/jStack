"""Stage 2 of plan.md: the most common words, built on stage 1's counter."""

from demo.wordcount import count_words  # noqa: F401 — stage 2 builds on it


def top(text, n):
    """The `n` most common words in `text` as `[(word, count), ...]`.

    Highest count first; equal counts in alphabetical order. Words are exactly
    what `count_words` says they are.
    """
    raise NotImplementedError("stage 2 of plan.md")
