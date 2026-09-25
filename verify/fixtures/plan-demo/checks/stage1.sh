#!/bin/sh
# Stage 1's gate: count_words passes its tests.
exec python3 -m unittest -q tests.test_wordcount
