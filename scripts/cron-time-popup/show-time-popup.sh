#!/usr/bin/env bash
# Shows a desktop popup with the current time, ~10 seconds after being invoked.
#
# Cron itself only has minute-level granularity, so it cannot fire "10
# seconds from now" on its own. This script closes that gap: it sleeps 10
# seconds first, then displays the time *at that point*, so when it's
# scheduled for the top of the next minute the popup lands ~10s in.
set -euo pipefail

sleep 10

current_time="$(date)"
message="Current time: ${current_time}"

if command -v osascript >/dev/null 2>&1; then
    osascript -e "display dialog \"${message}\" with title \"Time Popup\" buttons {\"OK\"} default button \"OK\""
elif command -v notify-send >/dev/null 2>&1; then
    notify-send "Time Popup" "${message}"
    echo "${message}"
else
    echo "${message}"
fi
