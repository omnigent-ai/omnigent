# cron-time-popup

A tiny, self-contained utility that shows a desktop popup with the current
time, fired via a cron job roughly 10 seconds after being scheduled.

## What it does

- `show-time-popup.sh` — sleeps 10 seconds, then shows the current date/time
  in a native macOS dialog (`osascript`). On non-macOS systems it falls back
  to a `notify-send` desktop notification if available, or just echoes the
  time to stdout if neither is available.
- `install-cron.sh` — adds a one-shot crontab entry that runs
  `show-time-popup.sh` at the top of the next minute, then removes itself
  from the crontab so it only fires once.

## Caveat: cron's minute granularity vs. the 10-second sleep

Standard cron can only schedule at minute granularity — it has no way to
express "run in 10 seconds". `install-cron.sh` works around this by
scheduling the job for the *next* minute boundary (up to ~60 seconds away),
and `show-time-popup.sh` itself sleeps 10 seconds before showing the popup.
So the popup shows the time ~10 seconds after the script starts running, but
the script itself may start up to ~60 seconds after you run `install-cron.sh`
(whenever the next minute boundary hits), not exactly 10 seconds after
install.

## Install

```bash
./install-cron.sh
```

This adds a temporary entry to your user crontab for the next minute. Once
it fires, the popup appears (~10s later) and the entry is automatically
removed from the crontab.

## Remove manually (before it fires)

```bash
crontab -l | grep -v 'cron-time-popup' | crontab -
```

Or run `crontab -e` and delete the line tagged `# cron-time-popup`.

## Run directly (no cron)

```bash
./show-time-popup.sh
```

This skips scheduling entirely and just runs the sleep-then-popup sequence
immediately.
