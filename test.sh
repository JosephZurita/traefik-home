#!/usr/bin/env sh
set -eu
python -m unittest discover -s test -p 'test_*.py' -v
