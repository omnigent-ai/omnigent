# omnigent-guide eval

Measures whether an agent inside an installed Omnigent answers questions about
Omnigent correctly for the installed version, and which part of a change
causes any gain. Each build under test is an "arm": a non-editable install
built from an sdist, the way releases are built.

## Questions

`questions.yaml` holds real questions, mined from GitHub issues and
discussions and from one user's Omnigent session history (paraphrased and
scrubbed). The 269 questions were embedded, clustered with k-means
(`sample.py`), and drawn at random within clusters in proportion to cluster
size. A drawn question was replaced by the next draw from its cluster only
when the code and docs do not establish an answer: a bug whose only answer is
a code fix, a question about the asker's private state, a question about
another product, or a duplicate. Near misses are real questions about other
tools, used to measure loading the guide when it is not needed.

Each question's criteria come from the code, with a file and line per
criterion. A criterion with a `check` is decided by code; the rest by two LLM
judges from different model families. `questions.lock` freezes the test split;
`run.py sessions --split test` refuses to run if the split changed, and runs
each (arm, case, repeat) once. `cases.yaml` is the earlier hand-written set,
kept for regression.

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
does not exist in that build. Mentions the answer itself negates ("there is no
`omnigent schedule` command") do not count.

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
run.py freeze --arms OUT/arms.json --out RESULTS          # once, before any test run
run.py sessions --arms OUT/arms.json --out RESULTS --split dev --runs 1 \
  --harness openai-agents --arm a0 --arm a3 --model databricks-qwen35-122b-a10b --profile dev
run.py grade --arms OUT/arms.json --out RESULTS --profile dev --site-routes routes.txt \
  --judge llama=databricks-llama-4-maverick --judge gptoss=databricks-gpt-oss-120b
run.py report --arms OUT/arms.json --out RESULTS --baseline a0 --pair t3:t0 --pair s3:s0
run.py blind --out RESULTS        # sample for a maintainer to label
run.py agreement --out RESULTS --labels RESULTS/blind/labels.csv
```

`report` pairs each arm with the baseline by case and gives 95% bootstrap
intervals over cases for the pass rate, the fabrication rate, and how often
the guide loaded.

## Environment

- openai-agents runs give the agent's shell a `darwin_seatbelt` sandbox: it
  can run the arm's CLI and write only to its workspace and a scratch data
  dir, and it cannot signal other processes. Without that, `omnigent stop`
  in an agent's shell would stop whatever server answers on port 6767. Arm a4
  sets `allow_network: false`; the runner keeps its own gateway connection.
- claude-native runs use Claude Code's own login (a scratch Omnigent config
  with only a subscription provider), launch with `--permission-mode dontAsk`
  and an allowed-tools list, and load no user settings (`skills: none`).
  They cannot use the sandbox, so `shims/` refuses commands that would change
  the machine. Managed Claude Code settings on the machine still apply: for
  example, a hook that blocks `curl` to hosts outside an allowlist also
  blocks the guide's docs fetches.
