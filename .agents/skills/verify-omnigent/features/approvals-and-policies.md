# Approvals and policies

## Sub-features

- In-chat approval cards and standalone `/approve/{session}/{elicitation}` links
- Approval inbox, approve/reject, persistent approvals, and delegation
- AskUserQuestion and Exit Plan Mode forms
- Server, agent, and session policy attachment and enforcement

## How to get to it

Trigger an action that policy marks `ASK`, then respond from the chat card,
approval inbox, or standalone approval link. Open the agent-info popover or
settings to inspect and change policies.

## Driving it with Playwright

Use focused tests under `tests/e2e_ui/approvals` and
`tests/e2e_ui/agents/test_agent_info_popover.py`. Park a real permission request
at the server boundary, assert the pending card contents, click the visible
decision, and verify the waiting request receives the matching allow or deny
result. For remembered approvals, trigger a second matching request and prove
the intended scope, not a broader tool or domain, is bypassed.

The observable end state is both a resolved UI card and a drained server-side
elicitation with the exact decision.

## Gotchas

- Do not mock the approval card as already pending; park a real request.
- Shared editors may reject without being allowed to approve.
- WHS-backed Databricks permissions do not represent every OSS approval
  delegation field; unsupported values must degrade safely.
- A UI-only disabled state is insufficient; the server policy remains
  authoritative.
