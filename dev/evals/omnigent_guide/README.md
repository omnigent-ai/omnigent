# omnigent-guide eval

Measures whether an agent inside an installed Omnigent answers questions about
Omnigent correctly for the installed version, and which part of a change
causes any gain. Each build under test is an "arm": a non-editable install
built from an sdist, the way releases are built.

## Questions

`questions.yaml` holds real questions, mined from GitHub issues and
discussions (235) and from one user's Omnigent session history (34,
paraphrased and scrubbed). The 269 questions were embedded, clustered with
k-means into 14 clusters (`sample.py`), and 60 were drawn at random within
clusters in proportion to cluster size. A drawn question was replaced by the
next draw from its cluster only when the code and docs do not establish an
answer: a bug whose only answer is a code fix or which release fixed it, a
question about the asker's private state, a question about another product,
or a duplicate. `questions_skipped.yaml` lists the 18 replacements and why.
One cluster ran out, so 59 questions remain. 19 near misses, real questions
about other tools, measure loading the guide when it is not needed.

Each question's criteria come from the code, with a file and line per
criterion; criteria that differ between releases carry a `versions` range.
A criterion with a `check` is decided by code; the rest by two LLM judges
from different model families, which also grade the checked criteria so the
two can be compared. `questions.lock` freezes the 56-case test split (43
questions, 13 near misses) before any test session runs; `run.py sessions
--split test` refuses to run if the split changed. `cases.yaml` is the
earlier hand-written set, kept for regression.

## The referee

`referee.py` checks every claim in an answer against the build that produced
it, using facts `referee_probe.py` reads from that build:

- each `omnigent ...` invocation against the installed click tree (commands and flags),
- each agent-config YAML block, by loading it through the build's own spec
  loaders with key tracking, so keys the parser never reads are reported,
- keys for `omnigent config set` against the CLI's whitelist,
- tool, builtin, and harness names against the registries,
- `OMNIGENT_*` variables, doc paths, GitHub links (against the source tree at
  the linked ref), and omnigent.ai links (against the site's page list).

The fabrication rate is the share of answers with at least one claim that
does not exist in that build. An answer that names more commands has more
chances to invent one, so the report also gives the share of answers that name
an `omnigent` command and the share of named commands that do not exist,
pooled over all answers. Mentions the answer itself negates ("there is no
`omnigent schedule` command") do not count. Two near cases are reported
separately: a real key name in the wrong place (for example a single-file key
in a bundle `config.yaml`) counts as invalid config, and a name that only the
newest build has (`--latest`) counts as a version mismatch. A key name is
invented when the spec loaders never use it and, for a compound name such as
`omnigent_mcp`, nothing else in the package does either.

## Arms

`build_arms.sh OUT BASE HEAD` builds:

| Arm | Build |
| --- | --- |
| a0 | main before the change: no framework skills ship |
| a1 | a0 plus the package-data line only |
| a2 | the branch without the `load_skill` summaries |
| a3 | the branch |
| a4 | a3, with the agent's shell cut off from the network |
| t0, t3 | v0.18.0, without and with the branch's commits |
| s0, s3 | v0.17.0, without and with the branch's commits |

## Running

From the repository root, with every `OMNIGENT_*` session variable unset and
gateway credentials available (a Databricks profile):

```sh
run.py() { uv run --no-sync python -m dev.evals.omnigent_guide.run "$@"; }
run.py freeze --arms OUT/arms.json --validate-on a3 --validate-on s3  # once, before any test run
run.py sessions --arms OUT/arms.json --out RESULTS --split test --runs 2 \
  --harness openai-agents --arm a0 --arm a3 --model databricks-qwen35-122b-a10b --profile dev
run.py sessions --arms NATIVE_ARMS.json --out RESULTS --split test --runs 1 \
  --harness claude-native --arm a0 --arm a3
run.py grade --arms OUT/arms.json --out RESULTS --profile dev --site-routes routes.txt \
  --latest a3 --judge llama=databricks-llama-4-maverick --judge gptoss=databricks-gpt-oss-120b
run.py report --arms OUT/arms.json --out RESULTS --pair a3:a0 --pair a1:a0 --pair a2:a1 \
  --pair a3:a2 --pair a4:a3 --pair t3:t0 --pair s3:s0
run.py blind --out RESULTS --size 40   # sample for a maintainer to label
run.py agreement --out RESULTS --labels RESULTS/blind/labels.csv
```

`sessions` runs each (arm, case, repeat) once and retries only infrastructure
errors: gateway auth failures, rate limits, timeouts, and stacks that fail to
start. `report` pairs arms by case and gives 95% bootstrap intervals over
cases for the pass rate, the fabrication rate, and how often the guide
loaded. It also shows where agents looked things up (docs pinned to their own
release tag, main, `llms.txt`, CLI help) and how much lost score sits in
answers with a fabrication. A docs fetch counts as successful only when it
returned text, because a piped `curl -s` exits 0 with no output when it
fails. `grade` reuses judge verdicts for answers it graded before, so a
referee change re-grades every arm in about a minute.

`routes.txt` lists omnigent.ai paths, one per line, from the site's source
repository: its pages (`app/**/page.*`, `public/`) and the literal redirect
sources in `next.config.mjs` (for example `/install.sh`).

## Environment

- openai-agents runs give the agent's shell a `darwin_seatbelt` sandbox: it
  can run the arm's CLI and write only to its workspace and a scratch data
  dir, and it cannot read the rest of the home directory or signal other
  processes (`omnigent stop` would otherwise stop whatever server answers on
  port 6767). Arm a4 sets `allow_network: false`; the runner keeps its own
  gateway connection.
- The gateway limits input tokens per minute for each model, and an agent
  resends its context on every tool call, so more than two parallel lanes
  mostly trade sessions for rate-limit retries.
- claude-native runs use Claude Code's own login (a scratch Omnigent config
  with only a subscription provider) and load no user settings
  (`skills: none`). They cannot use the sandbox, so they launch with
  `--permission-mode dontAsk`, a short allowed-tools list, and deny rules for
  `~/Repositories`, `~/.omnigent`, and `~/.claude`; `shims/` refuses commands
  that would change the machine. Install the claude-native arms outside the
  checkout (for example under `~/.cache`) so `which omnigent` does not point
  at it, and pass them in their own arms file.
- Managed Claude Code settings on the machine still apply. For example, a
  hook that blocks `curl` to hosts outside an allowlist also blocks the
  guide's docs fetches in claude-native runs.
