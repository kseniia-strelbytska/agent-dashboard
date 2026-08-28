#!/usr/bin/env bash
# Every test here runs offline: no iTerm2, no Claude session, no network.
#
# pipefail matters: without it a suite's non-zero exit is masked by the `tail`
# it is piped into, and a red run reports itself as green.
set -uo pipefail
cd "$(dirname "$0")"

failed=0
run() {
  printf '== %-9s ==\n' "$1"
  if python3 "$2" | tail -"${3:-2}"; then :; else failed=1; fi
}

run layout   test_layout.py   1
run input    test_input.py    2
run flow     test_flow.py     2
run limits   test_limits.py   2
run memory   test_pressure.py 2
run retrofit test_retrofit.py 2
run usage    test_usage.py    2
run cat      test_cat.py      2
run bridge   test_bridge.py   2
run install  test_install.py  2

echo
if [ "$failed" -ne 0 ]; then
  echo "SUITES FAILED"
  exit 1
fi
echo "All suites passed."
