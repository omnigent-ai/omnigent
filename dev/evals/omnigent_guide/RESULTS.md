# omnigent-guide eval: results

Measured on 2026-10-15/16. Baseline main is `d22454b43`; the change is
`f85a823a2` (this branch's product commits). Method and commands are in
`README.md`; the frozen test split has SHA-256 `c2d4bad2…` (`questions.lock`).

## Setup

- **Questions:** 56 test cases scored once per arm: 43 real Omnigent questions
  (37 from GitHub issues and discussions, 6 from session history; 14 topic
  clusters) and 13 near misses about other tools. Each criterion cites the code
  or doc that establishes it.
- **openai-agents:** Qwen 3.5 122B on the Databricks gateway, 2 runs per case
  per arm (86 graded answers per arm). The agent's shell runs in a Seatbelt
  sandbox.
- **claude-native:** Claude Code with Sonnet on a Claude subscription, 1 run
  per case, main (`a0`) and the change (`a3`) only.
- **Grading:** code checks where a criterion has one, else the mean of two
  judges (Llama 4 Maverick, gpt-oss-120b). The referee checks every command,
  flag, config key, tool, env var, doc path, and URL against the build that
  answered. Intervals are 95% paired bootstraps over cases (10,000 resamples).
- **Arms:** `a0` main; `a1` packaging fix only; `a2` plus the guide;
  `a3` plus the `load_skill` summaries (the change); `a4` the change with the
  agent's network blocked; `t0`/`t3` and `s0`/`s3` are v0.18.0 and v0.17.0
  without and with the change.

## Results

### claude-native

| Arm | Pass rate | Answers with an invented name | Answers naming a command | Named commands that don't exist | Guide loaded | Loaded on near misses | Tokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| a0 | 29% | 5% | 35% | 5% | 0% | 0% | 49.6k |
| a3 | 38% | 0% | 86% | 0% | 93% | 0% | 53.8k |

a3 vs a0: pass rate +10 pp [-1, +21], answers with an invented name -5 pp
[-12, +0], named commands that don't exist -5 pp [-14, +0].

### openai-agents

| Arm | Pass rate | Pass rate, completed turns | Answers with an invented name | Answers naming a command | Named commands that don't exist | Turn failed on a bad tool call | Guide loaded | Loaded on near misses | Tokens |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a0 | 19% | 21% | 7% | 20% | 30% | 1% | 0% | 0% | 25.4k |
| a1 | 19% | 19% | 15% | 27% | 33% | 2% | 0% | 0% | 31.2k |
| a2 | 20% | 23% | 9% | 33% | 12% | 14% | 52% | 27% | 48.0k |
| a3 | 27% | 33% | 14% | 49% | 12% | 19% | 98% | 35% | 80.9k |
| a4 | 25% | 28% | 10% | 60% | 8% | 10% | 97% | 42% | 72.1k |
| t0 | 21% | 21% | 9% | 22% | 34% | 0% | 0% | 0% | 21.4k |
| t3 | 31% | 37% | 17% | 52% | 12% | 15% | 99% | 38% | 99.7k |
| s0 | 20% | 20% | 10% | 24% | 27% | 2% | 0% | 0% | 25.3k |
| s3 | 20% | 29% | 15% | 48% | 21% | 29% | 99% | 42% | 83.5k |

| Comparison | Pass rate Δ | Completed turns Δ | Answers with an invented name Δ | Named commands that don't exist Δ | Guide loaded Δ |
| --- | --- | --- | --- | --- | --- |
| a3 vs a0 (the change) | +8 pp [-2, +18] | +12 pp [+2, +23] | +7 pp [-1, +15] | -17 pp [-38, +6] | +83 pp [+73, +91] |
| a1 vs a0 (packaging) | -0 pp [-5, +5] | -2 pp [-7, +3] | +8 pp [+1, +16] | +4 pp [-11, +18] | +0 pp |
| a2 vs a1 (guide) | +2 pp [-4, +7] | +4 pp [-2, +10] | -6 pp [-16, +3] | -21 pp [-46, +3] | +46 pp [+36, +57] |
| a3 vs a2 (summaries) | +6 pp [-4, +17] | +10 pp [-2, +22] | +5 pp [-3, +13] | +0 pp [-13, +16] | +37 pp [+25, +48] |
| a4 vs a3 (no network) | -1 pp [-10, +8] | -6 pp [-17, +3] | -3 pp [-13, +6] | -4 pp [-16, +7] | +1 pp [-2, +4] |
| t3 vs t0 (v0.18.0) | +10 pp [+1, +18] | +15 pp [+8, +23] | +8 pp [+0, +17] | -22 pp [-47, +8] | +85 pp [+75, +93] |
| s3 vs s0 (v0.17.0) | -0 pp [-9, +9] | +9 pp [+1, +18] | +5 pp [-6, +15] | -6 pp [-30, +20] | +86 pp [+77, +94] |

"Named commands that don't exist" pools every `omnigent` command the answers
name; its interval resamples questions, so it is wide when inventions cluster
in a few questions.

## What improved

- The guide loads when it should: 93% (claude-native) and 98% (openai-agents)
  of real questions, from 0%. The summaries account for half of that on
  openai-agents (52% to 98%).
- On a tagged release (v0.18.0) the pass rate rose 10 pp [+1, +18].
- When the turn completes, answers improve on every build: +12 pp on main,
  +15 pp on v0.18.0, +9 pp on v0.17.0.
- On tagged builds the version-pinned docs path ran: 59% (v0.18.0) and 56%
  (v0.17.0) of answers fetched docs at their own tag, and 57% of answers got
  text back.
- On claude-native, answers name a command far more often (35% to 86%) and
  none of those commands is invented (from 5%).

## What did not improve

- **Pass rate on main is not significant:** +8 pp [-2, +18] on openai-agents
  and +10 pp [-1, +21] on claude-native.
- **No gain on v0.17.0:** -0 pp [-9, +9].
- **Failed turns:** with the guide, 10–29% of openai-agents turns end on an
  invalid tool call, against 0–2% without it. Qwen emits the guide's first
  step, `omnigent --version`, as a tool name (often with broken tool-call
  markup), and the openai-agents harness ends the turn. Claude Code never did.
- **Invented names on openai-agents did not fall:** answers with an invented
  name went from 7% to 14% on main (+7 pp [-1, +15]). With the guide, answers
  name a command more than twice as often (20% to 49%), so they have more
  chances to invent one. Per named command the share that does not exist fell
  (30% to 12% on main, 34% to 12% on v0.18.0), and the count of invented names
  on main hardly moved (14 to 15). Neither change is significant. Packaging
  alone (`a1`) raised the per-answer rate by as much (+8 pp [+1, +16]) without
  loading the guide, at the same per-command rate as main (33% against 30%).
- **Near misses:** openai-agents loads the guide for 35% of near misses
  (Claude Code hooks, Claude Code login, MCP, Claude artifacts); claude-native
  for none.
- **Agent configs:** openai-agents answers with the guide contain agent YAML
  more often (21 of 86 on main against 9), and about half of it does not load
  cleanly (9 against 2; 12 against 0 on v0.18.0).
- **Cost:** openai-agents uses 3.2× the tokens (25k to 81k); claude-native
  uses 8% more.
- **Docs fetches:** blocking the network made no measurable difference
  (-1 pp [-10, +8]). Agents often fetch paths that do not exist at that ref.
- **Version-specific facts:** only one test criterion depends on the version,
  too few to measure. The referee found 2 version mismatches, both on v0.17.0
  without the guide (`omnigent agent`, added in 0.18.0).

## Where the score is lost

Fabrication is not the main failure. On the change (openai-agents), answers
with a fabrication hold 13% of the lost score, failed turns 25%, and complete
answers that name nothing invented but are wrong or incomplete hold the other
62%. Answers with a fabrication score no lower than the rest (31% against
26%). Invented names are mostly subcommands (114 of 173 findings), such as
`omnigent session list`, `omnigent host add`, and `omnigent debug db-current`.

The guide changes which subcommands get invented. Over the four builds without
it, most are `omnigent debug db-*` commands (32 findings, against 2 over the
five builds with it). With it, most are `omnigent session list`, `delete`,
`info`, and `fork` (30 findings, against 7 without). `omnigent session` has only
`export` and `import` in every build tested and on current main, and no doc
names the others. The guide's task table lists `omnigent session` for "Resume
or manage sessions" without its subcommands. On the three builds with the
change, 32 of 36 invented subcommands come from answers whose agent ran no
`--help` at all, although the guide's first step tells it to.

Smallest code-derived fixes (proposed, not in this PR):

1. In the openai-agents harness, return an unknown tool name to the model as a
   tool error instead of ending the turn, as Claude Code does. This targets the
   largest loss the change introduces.
2. Generate a CLI reference from the click tree at build time and ship it in
   the guide, so one read shows every real subcommand and flag. This targets
   the unchecked subcommand guesses above, including the `omnigent session`
   ones, without adding prose to the guide.

## Grading checks

- **Judges against each other:** 88–89% agreement on openai-agents answers
  (Cohen's kappa 0.56 on code-checked criteria, 0.64 on judge-only), 83–88% on
  claude-native (kappa 0.61–0.70).
- **Judges against code checks:** Llama 85–95% (kappa 0.55–0.76), gpt-oss
  87–100% (kappa 0.50–1.0) on openai-agents.
- **Human labels:** a blind sample of 40 answers (140 labels) is ready for a
  maintainer (`run.py blind`); no labels yet.
- **Referee audit:** after the test runs I checked every counted finding on
  the test answers by hand. The referee had counted version output
  (`omnigent 0.19.0.dev0`), placeholders, shell redirects, `acp:<slug>`
  harness ids, program names, omnigent.ai redirects, and real keys in the
  wrong place as invented. The fixes (`7e4efc8fd`) apply to every arm alike.
  After them, each of the 102 distinct invented names on the test answers is
  absent from the build that answered.

## Limitations

- One model per harness. Qwen's tool-call formatting drives the failed turns;
  other models may behave differently.
- claude-native ran once per case. This machine's managed Claude Code settings
  block `curl` to non-allowlisted hosts, so claude-native fetched docs only
  through WebFetch (21% of answers got text).
- In the claude-native runs, `which omnigent` led to the guard shim inside the
  checkout. Agents saw the path but every read was denied. The shims now run
  from a temporary directory.
- Pass rates are low for every arm (19–38%): most test questions are
  troubleshooting reports where the code supports only part of an answer.
