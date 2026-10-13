# omnigent-guide eval

Measures whether agents inside Omnigent answer questions about Omnigent
correctly. It compares two installed builds: `baseline` (before a change) and
`treatment` (with it). Both builds run the same cases, with the same model and
the same agent, in fresh sessions. The method follows
[agentskills.io](https://agentskills.io/skill-creation/evaluating-skills):

- **Guide loaded**: how often a framework skill loads on cases that need it.
- **Loaded when it should not**: how often one loads on near-miss cases.
- **Answer pass rate**: the share of assertions that pass. Most are
  deterministic checks: regexes, tool calls, and a generated agent that must
  parse. A judge model grades the rest.
- **Tokens and time**: the cost of each answer.

`cases.yaml` is the answer key. Tune skills on `dev` cases only, and use
`holdout` cases to check that a change generalizes.

## Build the two arms

Build each wheel the way a release does: `uv build` makes the sdist, then
builds the wheel from it. An editable install, or a wheel built straight from
a checkout, can contain files that the released wheel leaves out, so neither
reproduces what users get.

```sh
mkdir -p /tmp/eval/base-src
git archive "$(git merge-base HEAD origin/main)" | tar -x -C /tmp/eval/base-src
for arm in base head; do
  src=$([ "$arm" = base ] && echo /tmp/eval/base-src || echo "$PWD")
  (cd "$src" && OMNIGENT_SKIP_WEB_UI=true uv build --out-dir "/tmp/eval/$arm-dist")
  uv venv --python 3.12 "/tmp/eval/$arm"
  # --no-sources stops the SDKs from pulling an editable omnigent from the checkout.
  uv pip install --no-sources --python "/tmp/eval/$arm/bin/python" \
    /tmp/eval/$arm-dist/omnigent-*.whl "$src/sdks/python-client" "$src/sdks/ui"
done
```

## Run

Run from the repository root. Unset every `OMNIGENT_*` variable first if you
are inside an Omnigent session. The eval needs gateway credentials, for
example a Databricks profile.

```sh
uv run --no-sync python -m dev.evals.omnigent_guide.run \
  --baseline-python /tmp/eval/base/bin/python \
  --treatment-python /tmp/eval/head/bin/python \
  --profile dev --out /tmp/eval/results \
  --harness openai-agents --model databricks-qwen35-122b-a10b \
  --judge-model databricks-llama-4-maverick
```

Harnesses:

- `openai-agents` needs no CLI and works with any chat model the gateway serves.
- `claude-native` and `claude-sdk` run the Claude Code CLI. A machine whose
  Claude Code managed settings pin sign-in to an organization refuses gateway
  credentials, so these two fail there.

Models default to the harness bench's test models. Pass `--model` and
`--judge-model` when your workspace serves other endpoints. Pick a judge from a
different model family than the model under test.

A preflight check stops the run unless the baseline lacks
`omnigent-guide/SKILL.md`, the treatment has it, and neither arm imports
`omnigent` from the checkout.

The full matrix starts several hundred sessions and takes hours. While you
iterate, use `--harness claude-sdk --runs 1 --split dev`. A rerun with the
same `--out` resumes where it stopped. `--regrade` rebuilds the report from
`runs.jsonl` without starting sessions.

## Output

- `runs.jsonl`: one graded run per line: the answer, the tool calls, the
  skills loaded, and each assertion with its evidence.
- `benchmark.json`: per-harness, per-arm statistics and deltas, for all
  cases and for holdout cases only.
- `summary.md`: the tables to paste into a pull request.

Each session runs in a scratch directory under `--out`, with its own
`CLAUDE_CONFIG_DIR` and `OMNIGENT_DATA_DIR`, so your Claude Code settings,
skills, and Omnigent data stay out of the runs.
