---
name: escalate
description: Produce a formatted escalation package — severity, impact summary, timeline, suggested owner, and recommended next steps — ready to paste into a Slack incident channel or PagerDuty. Use when the ticket is critical/high severity or when the user explicitly asks to escalate.
---

# escalate — format a ticket for human hand-off

Normally Cara classifies and drafts a reply in two steps. **escalate** goes
further: it assembles a ready-to-paste escalation package for handing the
ticket off to an on-call engineer or team lead.

## When to use

- User types `/escalate`.
- User asks to "escalate", "page someone", "create an incident", or
  "hand this off".
- The classifier returned `"severity": "critical"` and a fix is not yet
  confirmed applied.

## Inputs you need

Before generating the package, ensure you have:

- The raw ticket text.
- The classifier's structured findings (category, severity, root cause,
  key facts).
- The responder's draft reply (optional but include if it exists).

If the classify → respond flow has not been run yet for this ticket, run it
first (as per Cara's default flow), then continue here.

## Escalation package format

Produce the following block verbatim (so the on-call can paste it directly):

    ---
    🚨 ESCALATION — <category> / <severity>
    ---

    **Summary**
    <One sentence: what is broken, who is affected, and the business impact.>

    **Root cause hypothesis**
    <classifier's hypothesis — one sentence>

    **Key facts**
    <bullet list from classifier>

    **Timeline**
    - <HH:MM UTC>  Ticket received.
    - <HH:MM UTC>  Classified as <severity> by Cara.
    - <HH:MM UTC>  Draft reply sent to customer (if applicable).
    - <HH:MM UTC>  Escalation package generated.
    *(Add further timestamps if known from ticket context.)*

    **Suggested owner**
    <role or team most likely responsible, e.g. "Auth team", "Billing
     on-call", "Platform SRE">

    **Recommended next steps**
    1. Acknowledge the ticket to the customer (reply is ready above).
    2. <Next debugging or mitigation step, based on root cause.>
    3. Update this escalation thread every 30 min until resolved.
    4. Close out by posting a brief post-mortem note.

    **Draft customer reply (for reference)**
    <paste the responder's draft here if available>

    ---

## Notes

- Fill in `<HH:MM UTC>` using the current time; if you do not know the
  current time, use `[time]` as a placeholder and ask the user to fill it in.
- Do not invent key facts or a timeline that aren't in the ticket or the
  classifier output.
- If the classifier hasn't run yet, say so and run it before producing the
  package.
