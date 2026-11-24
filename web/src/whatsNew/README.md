# What's New entries

Each file in `entries/` is one day's user-facing release notes, bundled into
the web app and shown in the "What's New?" badge to users who haven't seen it.

- **Name:** `entries/YYYY-MM-DD.md`, the day the changes landed. One file per day.
- **Title:** the first line is a `# ` heading summarizing the day.
- **Body:** a few Markdown bullets, each saying what users can now do or what got
  better. Lead each with a short bold phrase. Plain language only: no commit SHAs,
  file paths, session IDs or ticket IDs.
- Skip days with nothing user-visible instead of writing an empty entry.

A browser remembers the newest entry date its user has seen (localStorage key
`omnigent:whats-new:last-seen`); entries dated after it count as new.
