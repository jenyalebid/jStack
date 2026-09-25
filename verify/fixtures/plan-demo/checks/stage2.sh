#!/bin/sh
# Stage 2's tightened gate: the report passes AND stage 1 still does — `top`
# is built on `count_words`, so a stage 2 that broke stage 1 is not done.
python3 -m unittest -q tests.test_wordcount && exec python3 -m unittest -q tests.test_report
