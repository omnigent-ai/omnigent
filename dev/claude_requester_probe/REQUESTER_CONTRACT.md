# Completing native Claude requester support

**Deferred follow-up.** Native Claude delegation with edit sharing is an
accepted release limitation. The work below is not a release prerequisite;
resume it when supported provider APIs justify further integration work.
Keep Chat and terminal typing available and refuse unverifiable delegation.

**The tool-to-turn link works. The input-to-turn link is still a provider
dependency.** This describes the required semantics, not APIs Claude currently
implements. Keep terminal typing and mid-turn steering; unknown work remains
unable to delegate.

## The user journey

| Input to Bryan's shared session | Required result |
| --- | --- |
| Bryan asks it to create a worker | The verified owner execution can delegate. |
| Pat steers Bryan's running work | Once consumed, that execution loses delegation authority. |
| Pat's message waits in the queue | Its own later execution stays Pat's; queue order does not authenticate it. |
| Bryan starts fresh work afterward | Only the new owner execution regains authority. A delayed call from Pat's execution remains blocked. |
| Bryan types in the terminal | Preserve typing. Bind an accepted input to authenticated terminal-writer evidence; mixed or unobserved writers remain unknown. |

## Required provider contract

Omnigent already authenticates Chat and terminal writers. Claude does not need
to know their account names. It needs to preserve an opaque input reference
through acceptance, queuing and consumption, outside model-visible content.

Illustrative payloads below are **proposed**, not existing Claude events:

```json
{"input_ref":"input-pat-2","outcome":"accepted","provider_session":"session-1","provider_incarnation":"process-1"}
{"turn_id":"turn-7","input_revision":2,"consumed_input_refs":["input-bryan-1","input-pat-2"]}
{"tool_use_id":"tool-9","turn_id":"turn-7","provider_incarnation":"process-1"}
```

The authenticated Omnigent channel registers each reference before delivery.
The reference locates that registration; it is not a credential. Terminal
submissions need the same registration boundary before execution. Refused
inputs never consume a reference. Ambiguous acknowledgements stay pending and
restrict affected executions; they are never retried as a fresh submission.

The consuming-input view must be complete and available before a tool can
delegate, with a revision/barrier for concurrent steering. A best-effort event
stream alone is insufficient: lost or reordered events must not conceal Pat's
input. Recheck the execution at dispatch, including retained calls, approval
retries and proxy reconnection. Preserve existing per-execution restrictions
after turn completion. A process replacement needs new evidence; a turn from
the old process cannot become an owner turn in the new one.

## Delivery order and evidence

| Gap | Next implementation | Acceptance evidence |
| --- | --- | --- |
| Accepted input → consumed turn | Obtain a supported provider receipt/snapshot contract. Do not infer it from text, latest sender, `asUser`, or active turn at submission. | Real CLI: queued, rewritten, refused and ambiguous inputs retain the right references. |
| Authenticated transport | Extend the existing mod-channel work in #11426 and reuse #12004 requester/terminal-writer plumbing. Its control-command ID currently does not travel into Claude consumption evidence. | Actual Chat and authenticated terminal → mod → MCP → server; Bryan allowed, Pat blocked. |
| Reset and recovery | Scope retained evidence to provider process/session and handle gaps. | `/clear`, resume, compaction, reload, interruption and reconnect; no old-call promotion. The prototype currently covers `/clear` in a real process and retained/reset observations at component level. |
| Provider-created helpers | Require verifiable parent execution and resumed-helper consumption. `agentId` identifies a helper but does not authenticate its creator. | Actual helper creation/resume with owner and collaborator input; otherwise refusal. |
| Compatibility | Negotiate capabilities, integrate with the existing channel, then select the supported CLI floor from evidence. | Old/missing plugin refuses delegation while typing still works; channel and observer coexist on the selected version. |
| Full product and live deployment | After the receipt prerequisite, run the complete server/host/native/MCP journey, then the authorized live canary and separate Universe integration. | Exact candidate, actual dependency pins and enabled gates; screenshots/transcripts plus destination-state checks. |

No database migration is needed for this probe. The eventual bounded receipt
registry can reuse the native requester architecture, but restart persistence
and retention must be designed against the actual provider contract.

## Why the current hooks are insufficient

`session.append` does provide a useful partial receipt: a direct trusted call
can retain its returned row UUID, including the final rewritten row. That
proves storage, not which turn consumed it. Observing a native prompt's append
also does not identify the authenticated input that caused it. Queue records
are not conversation rows. The probe observes these IDs without saving content.

`PromptSubmitResult` reports text/context/origin or a drop, without an accepted
input reference. `TurnStartInput` reports text and a turn ID, without consumed
input references. `SessionMessage` exposes transcript text and tools, not an
authenticated input receipt. A mod channel command's success proves at most
submission, not which execution consumed it. `asUser` changes presentation,
not the authenticated requester. These limits occur in both published types
and the declarations generated by CLI 2.1.295 and 2.1.309; version-specific results
belong in the PR evidence.

Sources: [public declarations](https://github.com/anthropics/claude-code/blob/main/mods/types/claude-code.d.ts),
[mod events](https://code.claude.com/docs/en/plugins/mods/events),
[existing mod channel](https://github.com/omnigent-ai/omnigent/pull/11426),
[requester plumbing](https://github.com/omnigent-ai/omnigent/pull/12004).
