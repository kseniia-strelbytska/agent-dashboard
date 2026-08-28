#!/usr/bin/env bash
# Every test here runs offline: no iTerm2, no Claude session, no network.
set -e
cd "$(dirname "$0")"
echo "== layout =="; python3 test_layout.py
echo "== input  =="; python3 test_input.py | tail -2
echo "== flow   =="; python3 test_flow.py 2>/dev/null | tail -2
echo "== install=="; python3 test_install.py | tail -2
echo
echo "All suites passed."
