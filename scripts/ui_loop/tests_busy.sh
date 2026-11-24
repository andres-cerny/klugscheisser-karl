#!/usr/bin/env bash
# Is another session running pytest on the same test database? Prints "busy" or "free".
# Each worktree uses its own test DB (TEST_DATABASE_URL=…/klugscheisser_test_<topic>, docs/testing.md), so this
# only matters when two sessions picked the same name. Without an argument it checks $TEST_DATABASE_URL
# (default klugscheisser_test).
# A script file on purpose: an inline `pgrep -f pytest` also matches the calling shell's own command line.
# Usage: bash scripts/ui_loop/tests_busy.sh [db name]   |   wait: until [ "$(bash scripts/ui_loop/tests_busy.sh)" = free ]; do sleep 60; done
name_of() { local u="${1%%\?*}"; echo "${u##*/}"; }
want="${1:-$(name_of "${TEST_DATABASE_URL:-postgresql+psycopg://localhost/klugscheisser_test}")}"
for pid in $(pgrep -f "python[0-9.]* [^ ]*bin/py[t]est"); do
  url=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | sed -n 's/^TEST_DATABASE_URL=//p')
  [ "$(name_of "${url:-postgresql+psycopg://localhost/klugscheisser_test}")" = "$want" ] && { echo busy; exit 0; }
done
echo free
