# Word report

## Stage 1 — count words
Verify: command · sh checks/stage1.sh
Implement `count_words` in `demo/wordcount.py` so `tests/test_wordcount.py`
passes. Nothing under `tests/` or `checks/` changes.

## Stage 2 — report the most common words
Verify: command · python3 -m unittest -q tests.test_report
Implement `top` in `demo/report.py` on top of `count_words` so
`tests/test_report.py` passes. Nothing under `tests/` or `checks/` changes.
