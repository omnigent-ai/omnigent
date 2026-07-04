# Forge CLI spike

Date: 2026-07-04

Installed with:

```sh
curl -fsSL https://forgecode.dev/cli | sh
```

Installer result:

```text
Installing Forge and dependencies (fzf, bat, fd)...
Detected platform: x86_64-unknown-linux-gnu
Installing to /root/.local/bin/forge...

✓ Forge has been successfully installed!
forge 2.13.16
Run 'forge' to get started.

Installation complete!
Tools installed: forge, fzf, bat, fd
```

## Help output

`forge --help` verified the non-interactive prompt interface:

```text
Usage: forge [OPTIONS] [COMMAND]

Options:
  -p, --prompt <PROMPT>
          Direct prompt to process without entering interactive mode.

          When provided, executes a single command and exits instead of starting an interactive session. Content can also be piped: `cat prompt.txt | forge`.

      --conversation <CONVERSATION>
          Path to a JSON file containing the conversation to execute

      --conversation-id <CONVERSATION_ID>
          Conversation ID to use for this session.

          When provided, resumes or continues an existing conversation instead of generating a new conversation ID.

  -C, --directory <DIRECTORY>
          Working directory to use before starting the session.

      --sandbox <SANDBOX>
          Name for an isolated git worktree to create for experimentation

      --verbose
          Enable verbose logging output

      --agent <AGENT>
          Agent ID to use for this session

  -e, --event <EVENT>
          Event to dispatch to the workflow in JSON format
```

`forge -p --help` is not a help form. It treats `--help` as the prompt and starts the prompt path.

## Prompt-mode run

Command, in an isolated home/work dir:

```sh
HOME=/tmp/opencode/forge-spike/home \
XDG_CONFIG_HOME=/tmp/opencode/forge-spike/home/.config \
/root/.local/bin/forge -p "say hi" -C /tmp/opencode/forge-spike/work
```

Raw output:

```text
[2K⠋ Migrating credentials 00s · Ctrl+C to interrupt[2K[2K● [05:05:11] ERROR: No such device or address (os error 6)

EXIT_CODE=0
```

Outcome: blocked before model invocation by credential migration / auth setup. Forge prints a terminal-formatted error but exits with status `0`, so the executor must treat empty stdout plus stderr/error-looking output as failure.

No assistant output format could be verified because the prompt path did not reach a model. `forge --help` does not list a porcelain/json output flag for `-p`; plain text stdout is assumed and marked unverified in executor comments.

## Provider and config surface

Provider help:

```text
Usage: forge provider [OPTIONS] <COMMAND>

Commands:
  login   Authenticate with an API provider
  logout  Remove provider credentials
  list    List available providers
```

`forge provider list --porcelain` works without credentials and prints a table. Sample rows:

```text
NAME                       ID                           HOST
Anthropic                  anthropic                    api.anthropic.com
OpenAI                     openai                       api.openai.com
OpenRouter                 open_router                  openrouter.ai
KimiCoding                 kimi_coding                  api.kimi.com
```

Config path in the isolated home:

```text
/tmp/opencode/forge-spike/home/.forge/.forge.toml
```

`forge config set model --help`:

```text
Set the active model and provider atomically

Usage: forge config set model [OPTIONS] <PROVIDER> <MODEL>
```

Attempting to set a model without credentials:

```text
⠋ Initiating authentication... 00s · Ctrl+C to interrupt
● [05:06:12] ERROR: API key input cancelled

EXIT_CODE=0
```

Official docs verified `$FORGE_CONFIG` as a config directory override. Forge looks for `$FORGE_CONFIG/.forge.toml`; the directory must exist. Official custom-provider docs show the default session model shape:

```toml
[session]
provider_id = "my-provider"
model_id = "meta-llama/Llama-3.3-70B-Instruct"
```

## Conversation ID

`forge conversation new` produced a UUID:

```text
c33ebe76-6045-446d-959d-1a7c0bba635a
```

Non-UUID IDs are rejected:

```text
error: invalid value 'omnigent-spike-1' for '--conversation-id <CONVERSATION_ID>': Invalid conversation id: invalid character: found `o` at 0

EXIT_CODE=2
```

Using the generated UUID still hit credential migration before model invocation:

```text
⠋ Migrating credentials 00s · Ctrl+C to interrupt
● [05:06:36] ERROR: No such device or address (os error 6)

EXIT_CODE=0
```

Two-turn semantic resume could not be verified without provider credentials.

## Unverified assumptions carried into code

- `# UNVERIFIED`: prompt-mode successful assistant output is plain text on stdout. No porcelain/json prompt output flag was found in `forge --help`.
- `# UNVERIFIED`: `HARNESS_FORGE_MODEL` accepts either `provider:model` for verified `[session] provider_id/model_id` synthesis, or a bare model id written as `[session].model_id` while preserving the user's configured provider.
