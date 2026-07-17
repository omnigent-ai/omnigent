# Omnigent Slack Bot

Slack Socket Mode bot that maps one Slack thread to one Omnigent session.

## Setup

1. Create a Slack app with Socket Mode enabled.
2. Add bot scopes for `app_mentions:read`, `chat:write`, and the history scopes needed for the channel types where the bot will run.
3. Install the app into the workspace.
4. Copy `.env.example` to `.env` and fill in Slack and Omnigent values.
5. Run the bot:

```bash
UV_CACHE_DIR=.uv-cache uv run omnigent-slack
```

Set `LOG_LEVEL=DEBUG` in `.env` when diagnosing why Slack events are not producing replies.

If Omnigent has no online runners, the bot launches one on an online host using
the current working directory as the workspace. Set `OMNIGENT_RUNNER_WORKSPACE`
when the host needs a different absolute path.

Mention the bot with a message to start a session:

```text
@your-bot help me inspect this failure
```

Replies in that Slack thread continue the same Omnigent session.

## Development

From the repository root, use the canonical task surface:

```bash
just setup
just check
just test
```

The Omnigent API reference used for implementation is stored at `docs/api-1.yaml`.
