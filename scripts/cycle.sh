#!/bin/sh
# Daily stop/start schedule (plan.md section 9), run by host cron at 02:30 UTC.
# Day counted from the MANUAL_LOGIN event. Power-on after a poweroff comes from
# the GCE instance schedule `soak-daily-start` (02:45 UTC).
cd "$(dirname "$0")/.." || exit 1
DAY=$(docker compose exec -T soak python src/db.py day) || exit 1
echo "$(date -u +%FT%TZ) cycle: day $DAY"
case "$DAY" in
  3) docker compose down && docker compose up -d ;;          # container restart, host stays up
  4|5|6|7) sudo systemctl poweroff ;;                         # cold boot = real stop-on-idle
  *) echo "no cycle today" ;;
esac
