# Model Routing Evaluation

Use this template to show that model routing chooses the least expensive model
that still delivers an acceptable result. Do not claim that a model is "most
suitable" from one successful run; compare representative tasks against a
predefined quality gate.

## Decision policy

| Task class | Default model | Escalate when |
| --- | --- | --- |
| Bounded discovery, triage, or simple lookup | `gpt-5.6-luna` | The answer fails the evidence or accuracy gate. |
| Standard implementation and focused tests | `gpt-5.6-terra` | The change crosses system boundaries, needs substantial rework, or fails review. |
| Ambiguous, high-risk, or architecture-sensitive work | `gpt-5.6-sol` | No escalation: this is the highest default tier. |

The policy is deliberately conservative: a lower tier is promoted only after
observable evidence shows it is insufficient for the task.

## Evaluation protocol

1. Select a representative, non-sensitive task set for each class.
2. Use the same prompt, repository revision, acceptance criteria, and command
   budget for every compared model.
3. Run each task with Luna, Terra, and Sol where safe and practical.
4. Have a reviewer score the result without knowing which model produced it.
5. Record pass/fail, rework, latency, tokens, and the actual billed cost.
6. Choose the least expensive model whose pass rate meets the agreed threshold.

Recommended gates:

- Discovery: all cited files and commands are relevant; no invented evidence.
- Implementation: acceptance tests pass and no blocking review issue is found.
- Complex work: the change is correct, explainable, and needs no avoidable
  escalation or rework.

## Per-task evidence record

Copy this block for every evaluated task.

```text
Task ID:
Repository revision:
Task class:
Prompt and acceptance criteria:
Models compared:
Reviewer and scoring rubric:

Model | Quality-gate result | Rework required | Latency | Input tokens | Output tokens | Actual cost
----- | ------------------- | --------------- | ------- | ------------ | ------------- | -----------
Luna  |                     |                 |         |              |               |
Terra |                     |                 |         |              |               |
Sol   |                     |                 |         |              |               |

Chosen default for this task class:
Reason: lowest-cost model that met the quality threshold.
Links: task run, PR, test output, and review.
```

## Summary metrics

Report these metrics by task class and reporting period:

| Metric | Definition |
| --- | --- |
| Quality-gate pass rate | Passed tasks divided by evaluated tasks. |
| Escalation rate | Tasks moved to a stronger model divided by started tasks. |
| First-pass completion | Tasks completed without rework or escalation. |
| Median latency | Median wall-clock time from dispatch to usable result. |
| Actual cost per completed task | Billed spend divided by quality-gate passes. |
| Avoided cost | Cost of the chosen routing policy compared with always using Sol. |

Use actual workspace billing data. Published API prices are useful for planning,
but may not match a subscription or enterprise agreement.

## Evidence from this repository

| Task | Model | Result | Why the assignment was reasonable |
| --- | --- | --- | --- |
| Locate `/goal` routing | Luna | Identified the parser and executor paths plus a targeted test. | Bounded code discovery with a small, verifiable answer. |
| Map `/goal` coverage | Luna | Identified command activation, stopped-state, and routed-event tests. | Focused test discovery; no code change or broad test execution needed. |
| Scan for routing risk | Luna | Identified stale-runner error handling as a concrete verification target. | A narrow evidence-based risk scan, suitable for a cost-sensitive tier. |

This is supporting evidence that Luna can handle bounded discovery. It is not,
by itself, proof of comparative quality; the evaluation protocol above provides
that proof.
