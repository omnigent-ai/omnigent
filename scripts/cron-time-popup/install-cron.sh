#!/usr/bin/env bash
# Installs a one-shot cron entry that fires ~10 seconds from now.
#
# Cron can only be scheduled at minute granularity, so this schedules
# show-time-popup.sh for the *next* minute; the script's own `sleep 10`
# then brings the popup close to the requested "10 seconds from install
# time". The cron entry removes itself from the crontab right after it
# runs, so it only fires once.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POPUP_SCRIPT="${SCRIPT_DIR}/show-time-popup.sh"
MARKER="cron-time-popup"

chmod +x "${POPUP_SCRIPT}"

next_minute="$(date -v+1M +%M 2>/dev/null || date -d '+1 minute' +%M)"
hour="$(date -v+1M +%H 2>/dev/null || date -d '+1 minute' +%H)"

# Self-cleaning: run the popup, then strip this line (tagged by $MARKER)
# out of the crontab so the entry only ever fires once.
cron_command="${POPUP_SCRIPT} >> /tmp/cron-time-popup.log 2>&1; crontab -l | grep -v '${MARKER}' | crontab -"
cron_line="${next_minute} ${hour} * * * ${cron_command} # ${MARKER}"

existing_crontab="$(crontab -l 2>/dev/null || true)"
printf '%s\n%s\n' "${existing_crontab}" "${cron_line}" | grep -v '^$' | crontab -

echo "Installed one-shot cron entry for ${hour}:${next_minute} (fires within the next ~60s, then sleeps 10s before showing the popup)."
echo "To remove it manually before it fires: crontab -l | grep -v '${MARKER}' | crontab -"
