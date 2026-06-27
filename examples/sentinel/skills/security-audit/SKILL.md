---
name: security-audit
description: Audit a codebase or directory for security issues (hardcoded secrets, injection, unsafe deserialization, weak crypto, authz gaps) and produce a structured findings report. Use when the user asks for a security review, an audit, or to check code for vulnerabilities. Report only — never fix.
---

# security-audit — review code for security issues, report only

## 1. Collect scope

Identify what to audit (a directory, a diff, a module). Gather it yourself with
sys_os_* / git — this is plumbing, not investigation.

## 2. Dispatch the scanner (purpose: explore / search)

Hand the scanner the scope; it reads source, manifests, history and returns
per-finding evidence. Do NOT sprawl across the repo yourself.

## 3. Synthesize the draft — FINDINGS TEMPLATE (must match orchestrator prompt)

For each finding:

    ### <Severity>: <short title>
    - **Severity**: Critical | High | Medium | Low | Info
    - **Location**: file:line
    - **Recommendation**: <fix guidance — describe it, never apply it>
    - **Confidence**: high | medium | low

## 4. Cross-vendor review (purpose: review)

Route the draft through the reviewer (codex, different vendor) to confirm true
positives and drop false positives. Fold in its verdicts.

## 5. Deliver

Present the final report. You REPORT; you never edit, patch, or fix code.
