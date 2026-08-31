# Cost-Aware Model Routing

## Executive summary

We route work to the least expensive model that meets a predefined quality
threshold. Lower-cost models handle bounded discovery and triage; standard
implementation uses a balanced tier; difficult or high-risk work uses the
frontier tier. A task is escalated only when objective evidence shows that its
current tier is insufficient.

## Why this matters

Using one frontier model for every task is unnecessarily expensive. At the
same time, selecting a low-cost model without measurement risks rework and
slower delivery. Our routing policy optimizes for completed, reviewed work,
not just cheap initial requests.

## The policy

| Work type | Default tier | Evidence required to keep or escalate |
| --- | --- | --- |
| Bounded discovery and triage | Luna | Accurate, cited result with no rework. Escalate on an evidence or accuracy failure. |
| Normal repository changes | Terra | Passing acceptance tests and review. Escalate for broader ambiguity or repeated rework. |
| Complex or high-risk changes | Sol | Use when the task already warrants frontier reasoning. |

## How we prove the policy works

We run comparable tasks on multiple tiers with the same repository revision,
prompt, acceptance criteria, and time budget. A blinded reviewer scores the
results. We retain the lowest-cost tier that clears the quality threshold and
record actual billed cost, latency, rework, tests, and review outcome.

The attached evaluation records demonstrate both sides of the decision:

- quality: pass rate, first-pass completion, review findings, and test results;
- efficiency: actual cost per completed task, latency, and escalation rate.

## Current repository example

Three targeted `/goal` routing discovery tasks completed on Luna. They located
the implementation path, the relevant tests, and a specific stale-runner risk
to verify. Those tasks were intentionally bounded and read-only, making a
cost-sensitive tier appropriate. This validates the assignment for discovery
work; future comparative runs will measure whether the same tier remains the
lowest-cost option that meets our quality gate.

## Leadership reporting cadence

Report monthly by task class:

1. Completion and quality-gate pass rate.
2. Cost per accepted task, compared with an always-Sol baseline.
3. Escalation and rework rate.
4. Notable examples where a lower tier succeeded or was correctly escalated.

The success criterion is simple: reduce cost without lowering the rate of
accepted, reviewed, working repository changes.
