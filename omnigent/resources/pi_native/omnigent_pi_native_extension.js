// Auto-generated Omnigent bridge extension for native Pi sessions.
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// Tuning for the TOOL_CALL policy long-poll (see evalNativePolicyHttp).
//
// The server's POST /policies/evaluate parks an ASK verdict server-side
// (URL-based elicitation) and holds the connection open until a human
// resolves it via the web UI, then returns a hard ALLOW/DENY — the same
// contract the Claude Code / Codex / Cursor native hooks rely on
// (omnigent.native_policy_hook.post_evaluate_with_retry, ~1-day client
// read budget). Node's global fetch (undici) caps a connection that
// receives no response headers at headersTimeout (~5 min by default), so
// a long human wait would sever the park mid-flight. We therefore bound
// each attempt with an AbortController well under that cap and, on the
// resulting abort (or a transient 5xx / connect error), re-POST the SAME
// _omnigent_elicitation_id so the server RE-ATTACHES to the existing
// parked elicitation instead of publishing a second approval card. This
// mirrors the re-attach idiom in post_evaluate_with_retry while staying
// resilient to undici's header timeout.
//
// _PARK_ATTEMPT_TIMEOUT_MS — per-request abort budget; kept under undici's
//   ~300s headersTimeout default so a parked request is retried (re-attach)
//   rather than failing as UND_ERR_HEADERS_TIMEOUT.
// _PARK_TOTAL_BUDGET_MS — overall ceiling on the park loop. The human-wait
//   itself is capped server-side by the deciding policy's ask_timeout, so
//   this is a long backstop that keeps a wedged client from waiting forever.
const _PARK_ATTEMPT_TIMEOUT_MS = 240_000;
const _PARK_TOTAL_BUDGET_MS = 86_400_000;
// Budget for retrying genuinely transient transport errors (connect
// refused / reset / 5xx) before failing CLOSED. Distinct from the long park
// budget: a server that is actually down should resolve quickly (fail
// closed) rather than hang, so the transient-error budget is short.
const _TRANSIENT_RETRY_BUDGET_MS = 30_000;
const _TRANSIENT_RETRY_INITIAL_BACKOFF_MS = 1_000;
const _TRANSIENT_RETRY_MAX_BACKOFF_MS = 10_000;
// A genuine connect error (refused / reset) throws fast — well under the
// per-attempt park timeout. A legitimate long-poll abort only throws once
// our own _PARK_ATTEMPT_TIMEOUT_MS timer fires (the server held the
// connection open the whole time). We use the attempt's elapsed wall-time
// to disambiguate the two even when controller.signal.aborted has already
// flipped true (the abort-timer-race in finding #3): only an attempt that
// survived to ~the per-attempt timeout is treated as a re-attachable park;
// anything that failed materially sooner is a genuine transport error and
// is charged against the transient budget (→ eventually fail CLOSED).
const _PARK_REATTACH_MIN_ELAPSED_MS = _PARK_ATTEMPT_TIMEOUT_MS - 5_000;
// Cap on consecutive raw POLICY_ACTION_ASK rounds that never collapse to a
// hard verdict. A writable session's ASK is resolved server-side (the gate
// parks and returns ALLOW/DENY); a *raw* ASK that keeps coming back means the
// gate cannot be satisfied here (e.g. a read-only caller). Mirror the Python
// native hook, which fails a stray ASK CLOSED rather than deferring — we cap
// the rounds and then deny, instead of riding the 24h park ceiling to a
// fail-open. The brief inter-round sleep means this bounds wall-time too.
const _MAX_RAW_ASK_ROUNDS = 50;
// Reason surfaced when the tool-call gate cannot obtain a usable verdict and
// fails CLOSED. PHASE_TOOL_CALL is the sole enforcement point for a native
// connector tool, so an unevaluable policy must block, not proceed — matching
// omnigent.policies.types.FAIL_CLOSED_PHASES and the Python native hook's
// fail_closed_hook_output(PreToolUse) → deny.
const _FAIL_CLOSED_REASON =
  "blocked: Omnigent policy server unreachable — failing closed (PHASE_TOOL_CALL)";

// Phases whose unobtainable verdict must FAIL CLOSED (block). Mirrors the
// server's omnigent.policies.types.FAIL_CLOSED_PHASES: only PHASE_TOOL_CALL
// qualifies — it is the sole pre-execution enforcement point, so an
// unevaluable policy must not let the call through. PHASE_TOOL_RESULT runs
// AFTER the tool already executed, so a never-resolving result gate fails
// OPEN (the side effect is already incurred; blocking only suppresses an
// already-produced output) — matching fail_closed_hook_output(PostToolUse) →
// None and the runner-side posture.
const _FAIL_CLOSED_PHASES = new Set(["PHASE_TOOL_CALL"]);

function failClosedVerdict(phase) {
  // Block verdict when an enforcement-boundary phase cannot be evaluated.
  // Tag the reason with the phase for diagnosability.
  const tag = phase || "PHASE_TOOL_CALL";
  const reason =
    tag === "PHASE_TOOL_CALL"
      ? _FAIL_CLOSED_REASON
      : `blocked: Omnigent policy server unreachable — failing closed (${tag})`;
  return { block: true, reason };
}

// The unobtainable-verdict outcome for one phase: a block (fail closed) for
// FAIL_CLOSED phases, else a proceed (fail open). PHASE_TOOL_RESULT fails open
// so a transient outage never suppresses a legitimately-produced result.
function unobtainableVerdict(phase) {
  if (_FAIL_CLOSED_PHASES.has(phase)) return failClosedVerdict(phase);
  return { block: false, reason: "" };
}

function mintEvaluateElicitationId() {
  // Namespaced + 32 lowercase hex chars to satisfy the server's
  // _EVALUATE_HOOK_ELICITATION_ID_RE (^elicit_evaluate_[0-9a-f]{32}$).
  return `elicit_evaluate_${crypto.randomBytes(16).toString("hex")}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function readConfig() {
  const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
  if (!configPath) return null;
  try {
    return JSON.parse(fs.readFileSync(configPath, "utf8"));
  } catch (_err) {
    return null;
  }
}

/**
 * Evaluate a policy phase for a native Pi tool via the Omnigent server's
 * session-level HTTP endpoint (POST /v1/sessions/{sessionId}/policies/evaluate).
 *
 * Drives BOTH enforcement checkpoints (SKILL.md harness checklist #6):
 *   - PHASE_TOOL_CALL  (before the tool runs) — fail CLOSED.
 *   - PHASE_TOOL_RESULT (after the tool ran, before the model consumes the
 *       output) — fail OPEN. pi-native's ``tool_result`` extension hook can
 *       replace/suppress a result before the model sees it, so the result
 *       checkpoint is a real ALLOW/ASK/DENY gate here (unlike Claude/Codex
 *       PostToolUse, where a committed result can only be annotated).
 *
 * This is the same endpoint used by the Claude Code, Codex, and Cursor native
 * hooks. It does NOT require an active Omnigent turn context on the harness
 * side — the endpoint evaluates against the session's full policy set directly.
 *
 * Verdict handling (parity with the native hooks):
 *   - POLICY_ACTION_DENY  → block (TOOL_CALL: block the call; TOOL_RESULT:
 *       suppress the result) with the policy reason.
 *   - POLICY_ACTION_ALLOW / UNSPECIFIED (the engine default when no policy
 *       matches) → proceed.
 *   - POLICY_ACTION_ASK   → the server resolves ASK by PARKING this request
 *       (URL-based elicitation: it publishes an approval card to the web UI
 *       and holds the connection until a human resolves it), then returns a
 *       hard ALLOW/DENY — so a writable session never observes a raw ASK here.
 *       The server parks ASK for TOOL_CALL, LLM_REQUEST, REQUEST and (for
 *       pi-native's interceptable result) TOOL_RESULT. The park is realized by
 *       a generous client read budget plus re-attach retries (see the _PARK_*
 *       tuning above): if undici severs a long park (headersTimeout) we re-POST
 *       the SAME _omnigent_elicitation_id so the server re-attaches to the
 *       existing elicitation instead of opening a second approval card. A
 *       LEGITIMATE long-poll re-attach (the server is reachable and still
 *       holding the connection) is the only case that may loop toward the long
 *       park ceiling. If a raw ASK ever does come back (e.g. a read-only caller
 *       that cannot park), we re-evaluate up to _MAX_RAW_ASK_ROUNDS and then
 *       apply the phase's unobtainable-verdict posture.
 *
 * When a usable verdict cannot be obtained (transient-retry budget exhausted,
 * raw ASK never collapses within _MAX_RAW_ASK_ROUNDS, or the long park ceiling
 * reached), apply the phase's posture via ``unobtainableVerdict(phase)``:
 * PHASE_TOOL_CALL fails CLOSED (it is the SOLE pre-execution enforcement point,
 * never re-checked server-side, so an unevaluable policy MUST block — matching
 * omnigent.policies.types.FAIL_CLOSED_PHASES and the Python native hook's
 * fail_closed_hook_output(PreToolUse) → deny). PHASE_TOOL_RESULT fails OPEN
 * (the tool already executed; suppressing a legitimately-produced result on a
 * transient outage would be worse than letting it through — matching
 * fail_closed_hook_output(PostToolUse) → None). A reachable server long-polling
 * a parked ASK is the deliberate exception for both: that is the human approval
 * window, not an outage, and we keep waiting (re-attaching) rather than giving
 * up.
 *
 * @param {object} config - Bridge config (serverUrl, sessionId, authHeaders).
 * @param {string} phase - Proto phase wire string, e.g. ``"PHASE_TOOL_CALL"``
 *   or ``"PHASE_TOOL_RESULT"``.
 * @param {object} data - The proto event ``data`` for this phase
 *   (TOOL_CALL: ``{name, arguments}``; TOOL_RESULT: ``{result}``).
 * @param {object} [requestData] - Original tool-call payload on TOOL_RESULT
 *   (``{name, arguments}``) so an ON RESULT policy can resolve the tool name /
 *   correlate input. Omitted for TOOL_CALL.
 * @returns {Promise<{block: boolean, reason: string} | null>} A verdict.
 *   ``{block: true}`` either from a DENY or from a fail-closed phase that could
 *   not be evaluated; ``{block: false}`` on ALLOW (or a fail-open phase that
 *   could not be evaluated); ``null`` only when the gate is structurally
 *   disabled (no server/session configured or no global fetch) so there is
 *   nothing to enforce against.
 */
async function evalNativePolicyHttp(config, phase, data, requestData) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function"
  )
    return null;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/policies/evaluate`;
  // Mint one stable re-attach id for this evaluation. Every (re)POST carries
  // it so a re-park lands on the SAME elicitation — no duplicate approval
  // card. Kept for the whole call, across both the park loop and any
  // transient-error retries.
  const elicitationId = mintEvaluateElicitationId();
  const event = {
    type: phase,
    target: "",
    data: data || {},
    context: {},
  };
  // request_data carries the originating tool call on the result phase so an
  // ON RESULT policy can read the tool name / correlate input (the server's
  // _build_evaluation_context resolves tool_name from request_data.name).
  if (requestData) event.request_data = requestData;
  const body = JSON.stringify({
    event,
    _omnigent_elicitation_id: elicitationId,
  });
  const reqHeaders = {
    "content-type": "application/json",
    ...(config.authHeaders || {}),
  };

  const parkDeadline = Date.now() + _PARK_TOTAL_BUDGET_MS;
  // Independent transient-error budget so a server that is actually down
  // resolves quickly (fail CLOSED) instead of riding the long park ceiling.
  let transientDeadline = Date.now() + _TRANSIENT_RETRY_BUDGET_MS;
  let transientBackoff = _TRANSIENT_RETRY_INITIAL_BACKOFF_MS;
  // Bound on consecutive raw ASK rounds (see _MAX_RAW_ASK_ROUNDS / finding #2).
  let rawAskRounds = 0;

  while (true) {
    if (Date.now() >= parkDeadline) {
      // Park ceiling reached (well past any sane ask_timeout): the gate is
      // wedged, not resolving. Apply the phase posture — a never-resolving
      // TOOL_CALL gate blocks (sole pre-execution point); a never-resolving
      // TOOL_RESULT gate fails open (the tool already ran).
      return unobtainableVerdict(phase);
    }
    // AbortController bounds each attempt under undici's headersTimeout so a
    // long park is retried (re-attach) rather than thrown as a header
    // timeout. AbortSignal.timeout would be terser but is newer; this form
    // works on every Node that ships global fetch.
    const controller =
      typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller
      ? setTimeout(() => controller.abort(), _PARK_ATTEMPT_TIMEOUT_MS)
      : null;
    const attemptStart = Date.now();
    let resp;
    try {
      resp = await fetch(url, {
        method: "POST",
        headers: reqHeaders,
        body,
        ...(controller ? { signal: controller.signal } : {}),
      });
    } catch (_err) {
      if (timer) clearTimeout(timer);
      // Distinguish a LEGITIMATE long-poll re-attach from a GENUINE transport
      // error. controller.signal.aborted alone is unreliable: once our
      // per-attempt timer has fired it reads true even if a real connect
      // reset raced the timer (finding #3). A genuine connect error throws
      // fast — well under _PARK_ATTEMPT_TIMEOUT_MS — whereas a real long-poll
      // only aborts once the timer fires after holding the connection open
      // the whole attempt. So require BOTH aborted AND that the attempt
      // survived ~to the per-attempt timeout before treating it as a re-park.
      const aborted = !!(controller && controller.signal.aborted);
      const elapsed = Date.now() - attemptStart;
      const isLongPollReattach =
        aborted && elapsed >= _PARK_REATTACH_MIN_ELAPSED_MS;
      if (isLongPollReattach) {
        // Re-park: the server held the connection the whole time, so re-POST
        // the SAME elicitation id immediately (no backoff). This is the human
        // approval window — keep waiting (bounded only by parkDeadline).
        // A completed long-poll is a healthy round-trip, so refresh the
        // transient budget the same way the ASK branch does. Without this the
        // entry budget expires during the first park, and a genuine transport
        // blip while the human is still deciding would fail closed with zero
        // retries instead of getting its full transient window.
        transientDeadline = Date.now() + _TRANSIENT_RETRY_BUDGET_MS;
        transientBackoff = _TRANSIENT_RETRY_INITIAL_BACKOFF_MS;
        continue;
      }
      // Genuine transport error (connect refused / reset, or an abort that
      // fired too early to be a real park) → charge it against the short
      // transient budget. When that budget is exhausted, apply the phase
      // posture (fail closed for TOOL_CALL, open for TOOL_RESULT).
      if (Date.now() + transientBackoff >= transientDeadline) {
        return unobtainableVerdict(phase);
      }
      await sleep(transientBackoff);
      transientBackoff = Math.min(
        transientBackoff * 2,
        _TRANSIENT_RETRY_MAX_BACKOFF_MS,
      );
      continue;
    } finally {
      if (timer) clearTimeout(timer);
    }

    if (!resp.ok) {
      // 5xx is transient (retry within budget, re-attaching); 4xx is final
      // (a bad request won't succeed on retry). Either way, an unevaluable
      // phase applies its posture (TOOL_CALL closed, TOOL_RESULT open).
      if (resp.status >= 500) {
        if (Date.now() + transientBackoff >= transientDeadline) {
          return unobtainableVerdict(phase);
        }
        await sleep(transientBackoff);
        transientBackoff = Math.min(
          transientBackoff * 2,
          _TRANSIENT_RETRY_MAX_BACKOFF_MS,
        );
        continue;
      }
      return unobtainableVerdict(phase);
    }

    let json;
    try {
      json = await resp.json();
    } catch (_err) {
      // Malformed body — not retryable, and we have no verdict. Apply posture.
      return unobtainableVerdict(phase);
    }

    const result = json && json.result;
    if (result === "POLICY_ACTION_DENY") {
      return {
        block: true,
        reason: json.reason || "blocked by Omnigent policy",
      };
    }
    if (result === "POLICY_ACTION_ASK") {
      // The gate did not park server-side (e.g. a read-only caller that cannot
      // open an elicitation) yet still wants approval. Re-evaluate
      // (re-attaching) so a gate that is about to collapse to a hard verdict
      // gets the chance — but bound it: a raw ASK that NEVER collapses must
      // not ride the 24h park ceiling. After _MAX_RAW_ASK_ROUNDS we apply the
      // phase posture, mirroring the Python native hook which denies a stray
      // ASK on the enforcement-boundary phase rather than deferring (an
      // unresolvable approval gate that eventually allows would defeat its
      // purpose); a TOOL_RESULT stray ASK fails open instead.
      rawAskRounds += 1;
      if (rawAskRounds >= _MAX_RAW_ASK_ROUNDS) {
        return unobtainableVerdict(phase);
      }
      // Reset the transient budget since this is a healthy round-trip, not a
      // failure, and give it a brief beat so we do not hot-loop a server that
      // keeps returning ASK.
      transientDeadline = Date.now() + _TRANSIENT_RETRY_BUDGET_MS;
      transientBackoff = _TRANSIENT_RETRY_INITIAL_BACKOFF_MS;
      await sleep(_TRANSIENT_RETRY_INITIAL_BACKOFF_MS);
      continue;
    }
    // ALLOW / UNSPECIFIED / anything else → proceed.
    return { block: false, reason: "" };
  }
}

/**
 * Build a Pi tool-result object from an MCP ``tools/call`` JSON-RPC response.
 *
 * Pi expects ``{ content: [{ type: "text", text }], isError }``. The Omnigent
 * MCP proxy returns a JSON-RPC envelope whose ``result`` carries an MCP
 * content array (``[{ type: "text", text }]``) on success, or a JSON-RPC
 * ``error`` object (with the MCP convention code -32000 for tool denials /
 * tool errors) on failure. Map both into a single text block so the Pi agent
 * can read the output — denials surface as a readable error rather than
 * wedging the loop.
 */
function piResultFromMcpResponse(json) {
  if (json && typeof json === "object" && json.error) {
    const msg =
      (json.error && json.error.message) || "Omnigent tool call failed";
    return { content: [{ type: "text", text: String(msg) }], isError: true };
  }
  const result = json && typeof json === "object" ? json.result : undefined;
  // An ``input_required`` envelope must never reach this mapper: it carries no
  // ``content`` array, so it would otherwise fall to the "unexpected shape"
  // branch and be returned as ``isError: false`` — a confusing elicitation blob
  // masquerading as a successful tool result. callOmnigentTool detects and
  // resolves the ASK round-trip BEFORE calling this; treat a stray one as a
  // fail-closed error so an unresolved approval never reports success.
  if (result && typeof result === "object" && result.resultType === "input_required") {
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call requires approval that was not resolved",
        },
      ],
      isError: true,
    };
  }
  if (result && Array.isArray(result.content)) {
    const parts = [];
    for (const block of result.content) {
      if (block && typeof block === "object" && typeof block.text === "string") {
        parts.push(block.text);
      }
    }
    const text = parts.join("\n");
    return {
      content: [{ type: "text", text: text || safeJsonStringify(result) }],
      isError: result.isError === true,
    };
  }
  // Unexpected shape: hand the raw result back so nothing is silently dropped.
  return {
    content: [{ type: "text", text: safeJsonStringify(result ?? json ?? {}) }],
    isError: false,
  };
}

/**
 * Extract the elicitation id + opaque requestState from an MCP
 * ``input_required`` (MRTR) envelope, or ``null`` when the response is not an
 * ``input_required`` result.
 *
 * The Omnigent server keys ``inputRequests`` by the server-minted elicitation
 * id (the MRTR spec lets the client read those keys; only ``requestState`` is
 * opaque), so the first key is the id the retry must echo back inside
 * ``inputResponses``.
 */
function mcpInputRequired(json) {
  const result = json && typeof json === "object" ? json.result : undefined;
  if (!result || typeof result !== "object" || result.resultType !== "input_required") {
    return null;
  }
  const inputRequests =
    result.inputRequests && typeof result.inputRequests === "object"
      ? result.inputRequests
      : {};
  const elicitationId = Object.keys(inputRequests)[0] || "";
  const requestState =
    typeof result.requestState === "string" ? result.requestState : "";
  return { elicitationId, requestState };
}

/**
 * POST a single JSON-RPC ``tools/call`` to the server's per-session MCP proxy
 * and return the parsed response (or a fail-closed Pi tool-result on a
 * transport/HTTP error). ``extraParams`` carries the MRTR retry fields
 * (``requestState`` + ``inputResponses``) on the approval retry; it is omitted
 * on the initial call.
 *
 * @returns {Promise<{json: object} | {piResult: object}>} ``json`` on a parsed
 *   200 response; ``piResult`` is a terminal fail-closed tool result the caller
 *   returns as-is.
 */
async function postMcpToolsCall(config, toolName, args, rpcId, extraParams) {
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/mcp`;
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: rpcId,
    method: "tools/call",
    params: { name: toolName, arguments: args || {}, ...(extraParams || {}) },
  });
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(config.authHeaders || {}),
      },
      body,
    });
    if (!resp.ok) {
      return {
        piResult: {
          content: [
            {
              type: "text",
              text: `Omnigent tool call failed: HTTP ${resp.status}`,
            },
          ],
          isError: true,
        },
      };
    }
    return { json: await resp.json() };
  } catch (err) {
    return {
      piResult: {
        content: [
          {
            type: "text",
            text: `Omnigent tool call failed: ${err && err.message ? err.message : String(err)}`,
          },
        ],
        isError: true,
      },
    };
  }
}

/**
 * Execute an Omnigent tool by POSTing a JSON-RPC ``tools/call`` request to the
 * server's per-session MCP proxy endpoint
 * (``POST /v1/sessions/{sessionId}/mcp``).
 *
 * This is the SAME endpoint the runner's ``ProxyMcpManager`` uses: the Omnigent
 * server evaluates TOOL_CALL / TOOL_RESULT policy and then forwards execution
 * to the runner's ``/mcp/execute`` (which dispatches the real ``sys_*`` tool on
 * the correct machine with the session's terminal/workspace). The extension
 * already carries ``serverUrl`` + ``sessionId`` + ``authHeaders`` in its config,
 * so no extra relay process is needed.
 *
 * **ASK policy / elicitation round-trip (MRTR).** When the tool is gated by an
 * ASK policy the proxy returns HTTP 200 with an ``input_required`` result
 * (``{resultType, inputRequests, requestState}``) instead of executing. We must
 * NOT hand that envelope to the model as a result — it neither prompts nor runs
 * the tool. Mirroring ``ProxyMcpManager.dispatch()``, we resolve the human
 * verdict and retry once with the decision in ``inputResponses``:
 *   1. Long-poll ``POST /policies/evaluate`` via ``evalNativePolicyHttp`` — the
 *      SAME server-side ASK park the non-bridged hook uses. It holds the
 *      connection until a human resolves the approval card and collapses to a
 *      hard ALLOW / DENY (the extension has no in-process approval Future like
 *      the runner, so the long-poll IS its park/resolve mechanism).
 *   2. Retry the ``tools/call`` ONCE with ``requestState`` + ``inputResponses:
 *      {elicitationId: {action: "accept" | "decline"}}``. The server re-evaluates
 *      TOOL_CALL policy on the retry (it does not trust the client's claim
 *      blindly), so a still-denied tool stays denied.
 *   3. Cap at one retry; fail CLOSED (``isError: true``, readable message) if the
 *      approval cannot be resolved or the proxy still asks after the retry.
 *
 * (Trade-off: the proxy's ASK already published one approval card, and the
 * evaluate long-poll publishes a second one — the extension cannot re-attach to
 * the proxy-minted elicitation, whose id is in a different namespace. The human
 * resolves the evaluate card to drive the verdict; the proxy card is orphaned.
 * This is a UX wrinkle, not a security gap: the tool only runs on a genuine
 * human accept, and the server re-checks policy on the retry.)
 *
 * Fail-safe: any transport/parse error resolves to a readable tool-result error
 * (``isError: true``) rather than throwing, so a server hiccup never wedges Pi's
 * agent loop.
 */
async function callOmnigentTool(config, toolName, args) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function"
  ) {
    return {
      content: [
        { type: "text", text: "Omnigent tool bridge is not configured" },
      ],
      isError: true,
    };
  }

  const first = await postMcpToolsCall(config, toolName, args, 1);
  if (first.piResult) return first.piResult;
  const initial = mcpInputRequired(first.json);
  if (initial === null) {
    // ALLOW / DENY / executed — map directly to a Pi tool result.
    return piResultFromMcpResponse(first.json);
  }

  // ── ASK: resolve the human verdict, then retry once (MRTR) ──────────
  if (!initial.elicitationId || !initial.requestState) {
    // Malformed input_required — cannot retry. Fail CLOSED.
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call requires approval but the server sent no resolvable elicitation",
        },
      ],
      isError: true,
    };
  }

  const verdict = await evalNativePolicyHttp(config, "PHASE_TOOL_CALL", {
    name: toolName,
    arguments: args || {},
  });
  if (verdict === null) {
    // The approval gate is structurally unavailable (no server/session/fetch,
    // or a transport error) — fail CLOSED rather than report false success.
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call requires approval but the policy server was unreachable",
        },
      ],
      isError: true,
    };
  }
  const action = verdict.block ? "decline" : "accept";

  const retry = await postMcpToolsCall(config, toolName, args, 2, {
    requestState: initial.requestState,
    inputResponses: { [initial.elicitationId]: { action } },
  });
  if (retry.piResult) return retry.piResult;
  if (mcpInputRequired(retry.json) !== null) {
    // The proxy asked again after one approval round — do NOT loop. Fail CLOSED.
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call still requires approval after one round — not retrying",
        },
      ],
      isError: true,
    };
  }
  return piResultFromMcpResponse(retry.json);
}

function textFromContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    const text =
      block.text || block.input_text || block.output_text || block.content;
    if (typeof text === "string") parts.push(text);
  }
  return parts.join("");
}

function textFromMessage(message) {
  if (!message || typeof message !== "object") return "";
  return textFromContent(
    message.content || message.parts || message.message || "",
  );
}

function safeJsonStringify(value) {
  try {
    return JSON.stringify(value ?? {});
  } catch (_err) {
    return String(value);
  }
}

function textFromToolResult(event) {
  if (!event || typeof event !== "object") return "";
  const text = textFromContent(event.content);
  if (text) return text;
  if ("result" in event) {
    const result = event.result;
    if (typeof result === "string") return result;
    if (result && typeof result === "object") {
      const resultText = textFromContent(result.content);
      if (resultText) return resultText;
    }
    return safeJsonStringify(result);
  }
  if ("details" in event) return safeJsonStringify(event.details);
  return "";
}

// The model-facing text when a TOOL_RESULT policy DENIES (suppresses) a
// result. The real tool output is discarded and replaced with this error so
// the model learns the result was withheld by policy and cannot act on the
// suppressed content. Mirrors the "policy-denied error to the model" contract
// in the harness checklist (SKILL.md #6, Tool result → DENY).
function deniedResultText(reason) {
  const detail = reason && String(reason).trim();
  return detail
    ? `[Omnigent policy] Tool result suppressed: ${detail}`
    : "[Omnigent policy] Tool result suppressed by policy.";
}

function contentBlocks(message) {
  if (
    !message ||
    typeof message !== "object" ||
    !Array.isArray(message.content)
  )
    return [];
  return message.content;
}

function fingerprint(text) {
  let hash = 5381;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  }
  return `${text.length}-${hash.toString(36)}`;
}

function messageRole(message) {
  if (!message || typeof message !== "object") return "";
  return String(message.role || message.type || "");
}

function toInt(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
}

/**
 * Lift token usage out of one Pi assistant message.
 *
 * Mirrors the non-native executor's ``_extract_pi_turn_usage``
 * (omnigent/inner/pi_executor.py): Pi (``@earendil-works/pi-coding-agent``)
 * carries a per-message ``usage`` object with ``input`` / ``output`` /
 * ``cacheRead`` / ``cacheWrite`` / ``totalTokens`` counts, and the message
 * carries the resolved ``model``. Pi's ``input`` is the NON-cached input
 * (Anthropic semantics) — ``cacheRead`` / ``cacheWrite`` are separate, so the
 * full input a turn sent is ``input + cacheRead + cacheWrite``.
 *
 * @returns {{input:number,output:number,cacheRead:number,cacheWrite:number,
 *   total:number,model:(string|null)}|null} the per-message counts, or
 *   ``null`` when ``message`` is not an assistant message carrying usage.
 */
function extractPiUsage(message) {
  if (!message || typeof message !== "object") return null;
  if (message.role !== "assistant") return null;
  const usage = message.usage;
  if (!usage || typeof usage !== "object") return null;
  const input = toInt(usage.input);
  const output = toInt(usage.output);
  const cacheRead = toInt(usage.cacheRead);
  const cacheWrite = toInt(usage.cacheWrite);
  // No countable tokens means Pi emitted an empty usage object — treat as "no
  // usage" so the server leaves the session unpriced rather than recording a
  // $0.00 turn (matches _aggregate_pi_turn_usage's empty-usage guard).
  if (!(input || output || cacheRead || cacheWrite)) return null;
  const rawModel = message.model;
  const model = typeof rawModel === "string" && rawModel ? rawModel : null;
  return {
    input,
    output,
    cacheRead,
    cacheWrite,
    total: toInt(usage.totalTokens),
    model,
  };
}

function headers(config) {
  return {
    "content-type": "application/json",
    ...(config.authHeaders || {}),
  };
}

async function postEvent(config, body) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function"
  )
    return;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/events`;
  try {
    await fetch(url, {
      method: "POST",
      headers: headers(config),
      body: JSON.stringify(body),
    });
  } catch (_err) {
    // Keep Pi responsive even if Omnigent is temporarily unavailable.
  }
}

async function patchExternalSessionId(config, nativeSessionId) {
  if (
    !nativeSessionId ||
    !config ||
    !config.serverUrl ||
    typeof fetch !== "function"
  )
    return;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}`;
  try {
    await fetch(url, {
      method: "PATCH",
      headers: headers(config),
      body: JSON.stringify({ external_session_id: nativeSessionId }),
    });
  } catch (_err) {}
}

function setOmnigentStatus(config, ctx, state) {
  if (!ctx || !ctx.ui || !config) return;
  const urlLabel = config.conversationUrl
    ? `Omnigent: ${config.conversationUrl}`
    : "Omnigent";
  const label = state ? `${urlLabel} · ${state}` : urlLabel;
  try {
    ctx.ui.setTitle(`Omnigent: ${config.sessionId}`);
    ctx.ui.setStatus("omnigent", label);
    ctx.ui.setStatus("omnigent_state", undefined);
  } catch (_err) {}
}

function interruptActiveContext(ctx) {
  if (!ctx || typeof ctx.abort !== "function") return false;
  try {
    ctx.abort();
    return true;
  } catch (_err) {
    return false;
  }
}

/**
 * Trigger Pi's own context compaction on the resident ExtensionContext.
 *
 * Pi owns its context window inside this TUI process, so explicit /compact
 * must run here (the Omnigent server's AP-side compaction would only
 * summarise the transcript mirror and desync the two). ctx.compact() is
 * fire-and-forget (returns void); Pi summarises older messages and appends a
 * CompactionEntry to the session. We bracket it with external_compaction_status
 * events the server republishes as response.compaction.* SSE, so the web UI's
 * "Compacting conversation…" spinner tracks Pi's real progress.
 *
 * The server raises the spinner on the in_progress SSE and dismisses it on
 * completed/failed, so a completed/failed that reaches the server before
 * in_progress strands the spinner. ctx.compact() may invoke its callbacks
 * synchronously, so in_progress is AWAITED before the call: the server then
 * holds the spinner-raising edge before any terminal edge can post.
 *
 * Async and self-contained: the poller discards the returned promise, so every
 * edge is published here, never by the caller. Three outcomes:
 *   - No resident compaction API (ctx missing or ctx.compact not a function):
 *     post a visible error item so a user's /compact does not silently vanish
 *     (the runner already returned 200, so the server runs no fallback), post
 *     no spinner edge, return false.
 *   - ctx.compact() threw synchronously: in_progress was already posted, so the
 *     catch posts failed to dismiss the spinner, return false.
 *   - Submitted: in_progress posted and awaited; completed/failed follows from
 *     Pi's onComplete/onError, return true.
 */
async function triggerCompaction(config, ctx, customInstructions) {
  if (!ctx || typeof ctx.compact !== "function") {
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: `pi-compact-unavailable-${Date.now()}`,
        item_type: "error",
        item_data: {
          source: "execution",
          code: "pi_compact_unavailable",
          message:
            "Omnigent: /compact is unavailable for this Pi session. The " +
            "resident Pi context exposes no compaction API, so the model or " +
            "Pi version may not support it.",
        },
      },
    });
    return false;
  }
  const options = {
    onComplete: () => {
      postEvent(config, {
        type: "external_compaction_status",
        data: { status: "completed" },
      });
    },
    onError: (_error) => {
      postEvent(config, {
        type: "external_compaction_status",
        data: { status: "failed" },
      });
    },
  };
  if (typeof customInstructions === "string" && customInstructions.trim()) {
    options.customInstructions = customInstructions;
  }
  try {
    await postEvent(config, {
      type: "external_compaction_status",
      data: { status: "in_progress" },
    });
    ctx.compact(options);
    return true;
  } catch (_err) {
    await postEvent(config, {
      type: "external_compaction_status",
      data: { status: "failed" },
    });
    return false;
  }
}

function startInboxPoller(pi, config, handleInterrupt, handleCompact) {
  if (!config || !config.inboxDir || pi.__omnigentInboxPoller) return;
  // Bound the dedup set (FIFO eviction) — delivered files are unlinked, so a
  // long-lived TUI mustn't grow it unboundedly.
  const seen = new Set();
  const SEEN_CAP = 4096;
  const rememberSeen = (id) => {
    seen.add(id);
    while (seen.size > SEEN_CAP) seen.delete(seen.values().next().value);
  };
  // Cap send attempts so a persistently-failing sendUserMessage can't
  // re-read+re-throw the same file forever (the turn is already reported done).
  const deliverAttempts = new Map();
  const MAX_DELIVER_ATTEMPTS = 5;
  pi.__omnigentInboxPoller = setInterval(() => {
    let files = [];
    try {
      files = fs
        .readdirSync(config.inboxDir)
        .filter((name) => name.endsWith(".json"))
        .sort();
    } catch (_err) {
      return;
    }
    for (const file of files) {
      const fullPath = path.join(config.inboxDir, file);
      let payload;
      try {
        payload = JSON.parse(fs.readFileSync(fullPath, "utf8"));
      } catch (_err) {
        continue;
      }
      // Dedup only on a real string id; seen.has(undefined) would drop every
      // later id-less payload.
      const id = typeof payload?.id === "string" ? payload.id : null;
      if (!payload || (id !== null && seen.has(id))) {
        try {
          fs.unlinkSync(fullPath);
        } catch (_err) {}
        continue;
      }
      if (
        payload.type === "user_message" &&
        typeof payload.content === "string"
      ) {
        try {
          pi.sendUserMessage(payload.content, { deliverAs: "followUp" });
        } catch (_err) {
          // Leave the file to retry next tick, capped by attempt count.
          const key = id ?? fullPath;
          const attempts = (deliverAttempts.get(key) ?? 0) + 1;
          if (attempts < MAX_DELIVER_ATTEMPTS) {
            deliverAttempts.set(key, attempts);
            continue;
          }
          // Cap reached: surface the dropped follow-up without faking a turn
          // failure. The runner treats external_session_status:failed as
          // terminal for native sub-agents, so use a non-content conversation
          // error item and consume the file to stop the spin. Include the
          // message id and a short content preview so an operator can identify
          // what was lost (data loss; the file is unlinked below).
          deliverAttempts.delete(key);
          const droppedId = id ?? "(no id)";
          const preview =
            typeof payload.content === "string"
              ? payload.content.length > 80
                ? `${payload.content.slice(0, 80)}…`
                : payload.content
              : "";
          postEvent(config, {
            type: "external_conversation_item",
            data: {
              response_id: `pi-deliver-dropped-${Date.now()}`,
              item_type: "error",
              item_data: {
                source: "execution",
                code: "pi_followup_delivery_dropped",
                message:
                  `Omnigent: a queued follow-up message (id ${droppedId}) could ` +
                  `not be delivered to Pi after ${MAX_DELIVER_ATTEMPTS} attempts ` +
                  `and was dropped. Content preview: ${JSON.stringify(preview)}`,
              },
            },
          });
          try {
            fs.unlinkSync(fullPath);
          } catch (_err) {}
          continue;
        }
        deliverAttempts.delete(id ?? fullPath);
      }
      if (payload.type === "interrupt") {
        // An interrupt is point-in-time: make one delivery attempt, then
        // always consume the file (below). If there is no live turn to abort
        // right now, the interrupt is simply dropped — leaving the file would
        // re-read it every tick forever and, once a later turn creates an
        // abortable context, abort that unrelated turn. requestInterrupt only
        // arms the pendingInterrupt window when it catches a genuinely running
        // turn (idle interrupts are dropped, not armed — see F18), so a turn
        // already in flight still gets aborted via replay without poisoning the
        // next freshly-started turn.
        if (typeof handleInterrupt === "function") handleInterrupt();
      }
      if (payload.type === "compact") {
        // Point-in-time like an interrupt: one delivery attempt against the
        // resident context, then always consume the file (below) — leaving it
        // would re-trigger compaction every tick. handleCompact owns every
        // status edge and the unavailable-context error item, so its returned
        // promise is intentionally discarded: there is nothing for the poller
        // to retry or clean up.
        if (typeof handleCompact === "function") {
          handleCompact(
            typeof payload.custom_instructions === "string"
              ? payload.custom_instructions
              : undefined,
          );
        }
      }
      if (id !== null) rememberSeen(id);
      try {
        fs.unlinkSync(fullPath);
      } catch (_err) {}
    }
  }, 250);
}

module.exports = function (pi) {
  const config = readConfig();
  let sequence = 0;
  let turnOrdinal = 0;
  let activeResponseId = null;
  // Dedicated loop-state flag, set on agent_start / cleared on agent_end. Used
  // as the no-isIdle() fallback for requestInterrupt instead of
  // !activeResponseId: agent_start resets activeResponseId to null and only
  // turn_start assigns it, so an interrupt landing in that gap (after
  // agent_start, before turn_start) would look idle by activeResponseId yet the
  // loop is genuinely running — agentRunning arms it correctly. See F18.
  let agentRunning = false;
  let latestContext = null;
  let pendingInterruptUntil = 0;
  const postedToolCalls = new Set();
  const postedToolResults = new Set();
  const postedReasoning = new Set();
  const toolCallsById = new Map();
  const pendingInterruptMs = 30_000;
  // Live streaming state for assistant text deltas. Pi emits
  // message_update events carrying an assistantMessageEvent of type
  // "text_delta" (token chunk) / "text_end" (block complete) during a
  // turn — see @earendil-works/pi-ai AssistantMessageEvent. We forward
  // each token as a transient external_output_text_delta so the web UI
  // paints a live preview before the final message lands.
  //
  // The preview is keyed by ASSISTANT MESSAGE, not per text block: the
  // web UI (chatStore.pumpStreamEvents) finalizes the OLDEST in-flight
  // "live:<message_id>" preview when the authoritative text_done arrives,
  // FIFO, and message_end posts ONE combined external_conversation_item
  // per assistant message (textFromMessage joins all text blocks). So a
  // 1:1 message-scoped id keeps exactly one preview per item; a per-block
  // id would orphan extra previews when a message has multiple text
  // blocks (e.g. text → tool call → more text). All text blocks of a
  // message share its id with a single monotonic chunk index, so the
  // preview reads as one growing message — matching claude-native.
  //
  // Deltas are best-effort live preview: postEvent fails open, and the
  // authoritative text still arrives via message_end regardless.
  //
  // streamingMessageOrdinal: bumped at each assistant message_end so the
  // NEXT message of the turn gets a fresh, stable id distinct from earlier
  // ones — see the message_end handler for why it advances there (not on
  // message_start) so deltas and the finalize agree on the id.
  let streamingMessageOrdinal = 0;
  // streamedTextIndex: message_id -> next 0-based chunk index.
  const streamedTextIndex = new Map();
  // finalizedTextBlocks: message_ids whose final delta was already posted,
  // so a duplicate text_end (or a stray text_delta after end) can't reopen
  // or double-finalize the preview.
  const finalizedTextBlocks = new Set();

  // Names of the Omnigent tools registered via pi.registerTool below. Bridged
  // tools are policy-evaluated server-side inside the /mcp proxy (TOOL_CALL +
  // TOOL_RESULT), so the tool_call hook must NOT also call
  // evalNativePolicyHttp for them — that would double-evaluate and, for ASK
  // policies, double-prompt. Pi's OWN built-in tools (read/shell/etc) are not
  // in this set and stay gated by the hook below.
  const bridgedTools = new Set();
  if (config && Array.isArray(config.tools)) {
    for (const tool of config.tools) {
      if (!tool || typeof tool !== "object") continue;
      const name = typeof tool.name === "string" ? tool.name : "";
      if (!name) continue;
      bridgedTools.add(name);
      const description =
        typeof tool.description === "string" ? tool.description : "";
      const parameters =
        tool.parameters && typeof tool.parameters === "object"
          ? tool.parameters
          : { type: "object", properties: {} };
      // Pi passes tool.parameters straight to the LLM as JSON Schema, so the
      // Omnigent schema is usable as-is. execute() round-trips the call to the
      // Omnigent server's MCP proxy and returns the result to Pi.
      if (typeof pi.registerTool === "function") {
        pi.registerTool({
          name,
          label: name,
          description,
          promptSnippet: description ? description.slice(0, 120) : name,
          parameters,
          async execute(_toolCallId, params) {
            return callOmnigentTool(config, name, params || {});
          },
        });
      }
    }
  }

  // Cumulative session token usage. Pi reports PER-MESSAGE counts (one
  // assistant message per LLM call); session billing is their SUM — each call
  // is billed for the full context it re-sent, so summing per-message inputs is
  // the correct cumulative input. The server applies vendor pricing to these
  // cumulative totals and republishes a ``session.usage`` event (the SAME
  // contract claude-native / codex-native / cursor-native use), so the web
  // Session-cost badge + per-model token breakdown light up with no
  // server/frontend changes. Dedup by message fingerprint so a re-emitted
  // ``message_end`` / ``turn_end`` / ``agent_end`` carrying the same assistant
  // message never double-counts. ``usageModel`` tracks the latest message's
  // model (mirrors a mid-session model switch). ``lastPostedUsageKey`` dedups
  // the POST itself so a flush with no new tokens is skipped.
  const countedUsageMessages = new Set();
  let cumulativeInputTokens = 0;
  let cumulativeOutputTokens = 0;
  let cumulativeCacheReadTokens = 0;
  let usageModel = null;
  let lastPostedUsageKey = "";

  // Build a stable fingerprint for one assistant message so the same message
  // arriving on multiple lifecycle events is only counted once. Pi's
  // ``AssistantMessage`` (``@earendil-works/pi-ai``) carries NO ``id`` field
  // but DOES carry an optional provider ``responseId`` and a required numeric
  // ``timestamp`` — both stable across the same message's re-emission on
  // ``message_end`` / ``turn_end`` / ``agent_end``. Prefer those identity
  // fields (plus a forward-compat ``id``) over the usage-count fingerprint:
  // hashing counts alone collides two DISTINCT LLM calls that happen to report
  // identical token counts (e.g. two identical short acks under prompt
  // caching), which would silently drop the second call's tokens (undercount).
  // The usage-count fingerprint stays only as a last resort for a message that
  // carries no identity field at all.
  function usageMessageKey(message, usage) {
    if (message && typeof message === "object") {
      if (typeof message.id === "string" && message.id) return `id:${message.id}`;
      if (typeof message.responseId === "string" && message.responseId)
        return `rid:${message.responseId}`;
      if (typeof message.timestamp === "number")
        return `ts:${message.timestamp}`;
    }
    return `u:${usage.input}-${usage.output}-${usage.cacheRead}-${usage.cacheWrite}-${usage.total}-${usage.model || ""}`;
  }

  // Fold one assistant message's usage into the cumulative session totals,
  // deduped by fingerprint. Returns true when it counted (totals advanced).
  function accumulateUsage(message) {
    const usage = extractPiUsage(message);
    if (!usage) return false;
    const key = usageMessageKey(message, usage);
    if (countedUsageMessages.has(key)) return false;
    countedUsageMessages.add(key);
    // The server's ``cumulative_input_tokens`` is INCLUSIVE of cache reads (it
    // splits the cache portion back out and prices it at the cache-read rate),
    // so add cacheRead into the input total. ``cacheWrite`` (cache creation)
    // has no dedicated cumulative field on the server, so fold it into the
    // input total too — it is then priced at the input rate rather than the
    // ~1.25x cache-write rate, a small, documented approximation that never
    // drops the tokens.
    cumulativeInputTokens += usage.input + usage.cacheRead + usage.cacheWrite;
    cumulativeOutputTokens += usage.output;
    cumulativeCacheReadTokens += usage.cacheRead;
    if (usage.model) usageModel = usage.model;
    return true;
  }

  // POST the cumulative session usage so the server prices it and publishes a
  // ``session.usage`` event. Cumulative (SET) semantics — the server overwrites
  // its stored totals each flush. Deduped so a flush with no advance is a
  // no-op. Fail-open via ``postEvent`` so a failed POST never wedges Pi.
  async function postSessionUsage() {
    if (!(cumulativeInputTokens || cumulativeOutputTokens)) return;
    const postKey = `${cumulativeInputTokens}-${cumulativeOutputTokens}-${cumulativeCacheReadTokens}-${usageModel || ""}`;
    if (postKey === lastPostedUsageKey) return;
    lastPostedUsageKey = postKey;
    const data = {
      cumulative_input_tokens: cumulativeInputTokens,
      cumulative_output_tokens: cumulativeOutputTokens,
      cumulative_cache_read_input_tokens: cumulativeCacheReadTokens,
    };
    if (usageModel) data.model = usageModel;
    await postEvent(config, { type: "external_session_usage", data });
  }

  function rememberContext(ctx) {
    if (ctx) latestContext = ctx;
  }

  function newResponseId(prefix) {
    return `pi-${prefix}-${Date.now()}-${++sequence}`;
  }

  function currentResponseId() {
    if (!activeResponseId) activeResponseId = newResponseId("turn");
    return activeResponseId;
  }

  function hasPendingInterrupt() {
    if (!pendingInterruptUntil) return false;
    if (Date.now() > pendingInterruptUntil) {
      pendingInterruptUntil = 0;
      return false;
    }
    return true;
  }

  function safeIsIdle(ctx) {
    // Returns true/false from the SDK's isIdle(), or null when the signal is
    // unavailable (older SDK) or throws, so the caller can fall back.
    // Deliberately returns null (not true) on throw so callers fall back to loop
    // state (!agentRunning) rather than blindly treating the agent as idle.
    if (!ctx || typeof ctx.isIdle !== "function") return null;
    try {
      return ctx.isIdle();
    } catch (_err) {
      return null;
    }
  }

  function requestInterrupt(ctx) {
    // ctx.abort() is a silent no-op when the Pi agent is idle (it does NOT
    // throw), so an interrupt that arrives with no live turn must NOT arm the
    // replay window — otherwise the 30s window poisons the next legitimately
    // started turn (F18). Only arm when a turn is genuinely in-flight: prefer
    // the SDK's isIdle(), and fall back to the agent loop state on SDK versions
    // that don't expose it.
    const idle = safeIsIdle(ctx);
    const turnIsIdle = idle === null ? !agentRunning : idle;
    if (turnIsIdle) return false;
    const accepted = interruptActiveContext(ctx);
    if (!accepted) return false;
    pendingInterruptUntil = Date.now() + pendingInterruptMs;
    return true;
  }

  function replayPendingInterrupt(ctx) {
    if (!hasPendingInterrupt()) return false;
    interruptActiveContext(ctx);
    return true;
  }

  function clearPendingInterrupt() {
    pendingInterruptUntil = 0;
  }

  async function postToolCall(toolCall, responseId) {
    if (!toolCall || typeof toolCall !== "object") return;
    const callId = String(toolCall.id || toolCall.toolCallId || "");
    const name = String(toolCall.name || toolCall.toolName || "");
    if (!callId || !name) return;
    const key = `${responseId}:${callId}`;
    toolCallsById.set(callId, { key, responseId, name });
    if (postedToolCalls.has(key)) return;
    postedToolCalls.add(key);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "function_call",
        item_data: {
          agent: "Pi",
          name,
          arguments: safeJsonStringify(
            toolCall.arguments ?? toolCall.input ?? {},
          ),
          call_id: callId,
        },
      },
    });
  }

  async function postToolResult(event, responseId, outputOverride) {
    if (!event || typeof event !== "object") return;
    const callId = String(event.toolCallId || event.id || "");
    if (!callId) return;
    const known = toolCallsById.get(callId);
    const key = known && known.key ? known.key : `${responseId}:${callId}`;
    if (postedToolResults.has(key)) return;
    postedToolResults.add(key);
    // The mirror must reflect what the model actually consumed. When a
    // TOOL_RESULT policy suppressed the real output (DENY), the caller passes
    // the policy-error text as ``outputOverride`` so the Omnigent conversation
    // shows the same denied result the model saw — not the suppressed output.
    const output =
      typeof outputOverride === "string"
        ? outputOverride
        : textFromToolResult(event);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: known && known.responseId ? known.responseId : responseId,
        item_type: "function_call_output",
        item_data: {
          call_id: callId,
          output,
        },
      },
    });
  }

  async function postReasoningText(text, responseId, keyHint) {
    if (typeof text !== "string" || !text.trim()) return;
    const textKey = `${responseId}:text:${fingerprint(text)}`;
    const key = `${responseId}:${keyHint || fingerprint(text)}`;
    if (postedReasoning.has(key) || postedReasoning.has(textKey)) return;
    postedReasoning.add(key);
    postedReasoning.add(textKey);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "reasoning",
        item_data: {
          agent: "Pi",
          summary: [],
          content: [{ type: "reasoning_text", text }],
        },
      },
    });
  }

  function streamingMessageId(responseId) {
    // Stable per-assistant-message id across all of the message's text
    // chunks. The web UI keys an in-flight "live:<message_id>" preview off
    // this, appends each chunk in `index` order, and reconciles it against
    // the authoritative assistant item by FIFO retirement. responseId
    // scopes it to this turn and the ordinal distinguishes successive
    // assistant messages within the turn, so a finalized message's id is
    // never reused by a later one.
    return `${responseId}:msg:${streamingMessageOrdinal}`;
  }

  async function postOutputTextDelta(messageId, delta, options) {
    // Transient assistant-text chunk for live preview (Responses-style
    // response.output_text.delta on the wire). Not persisted; the
    // authoritative final text arrives separately via
    // external_conversation_item. A blank, non-final delta carries no
    // signal — skip it so an empty token can't churn the UI buffer.
    const final = !!(options && options.final);
    if (typeof delta !== "string") return;
    if (!delta && !final) return;
    const index = streamedTextIndex.get(messageId) || 0;
    streamedTextIndex.set(messageId, index + 1);
    await postEvent(config, {
      type: "external_output_text_delta",
      data: {
        delta,
        message_id: messageId,
        index,
        final,
      },
    });
  }

  async function postTextDelta(update, responseId) {
    // assistantMessageEvent of type "text_delta": one streamed token of
    // the current assistant message. All text blocks of the message share
    // its id, so the preview reads as one growing message.
    if (!update || typeof update.delta !== "string" || !update.delta) return;
    const messageId = streamingMessageId(responseId);
    if (finalizedTextBlocks.has(messageId)) return;
    await postOutputTextDelta(messageId, update.delta);
  }

  async function finalizeStreamingMessage(responseId) {
    // Emit a final-marker delta so the web UI knows no further chunks will
    // arrive for this message_id and can stop the live buffer. The marker
    // carries no new text (the running preview already holds the full
    // message); message_end posts the authoritative item that replaces the
    // preview in place. Only finalize a message we actually streamed (a
    // message with no text_delta has no live preview to close).
    const messageId = streamingMessageId(responseId);
    if (finalizedTextBlocks.has(messageId)) return;
    if (!streamedTextIndex.has(messageId)) return;
    finalizedTextBlocks.add(messageId);
    await postOutputTextDelta(messageId, "", { final: true });
  }

  async function mirrorAssistantMessage(message, responseId) {
    const blocks = contentBlocks(message);
    for (let index = 0; index < blocks.length; index += 1) {
      const block = blocks[index];
      if (!block || typeof block !== "object") continue;
      if (block.type === "toolCall") await postToolCall(block, responseId);
      if (block.type === "thinking") {
        const text = typeof block.thinking === "string" ? block.thinking : "";
        const key = block.thinkingSignature || `${turnOrdinal}:${index}`;
        await postReasoningText(text, responseId, key);
      }
    }
  }

  pi.registerCommand("omnigent", {
    description: "Show the Omnigent conversation URL",
    async handler(_args, ctx) {
      setOmnigentStatus(config, ctx, "linked");
      if (ctx && ctx.ui && config && config.conversationUrl) {
        ctx.ui.notify(`Omnigent: ${config.conversationUrl}`, "info");
      }
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    rememberContext(ctx);
    setOmnigentStatus(config, ctx, "linked");
    startInboxPoller(
      pi,
      config,
      () => requestInterrupt(latestContext),
      (customInstructions) =>
        triggerCompaction(config, latestContext, customInstructions),
    );
    const nativeSessionId =
      ctx && ctx.sessionManager && ctx.sessionManager.getSessionId
        ? ctx.sessionManager.getSessionId()
        : undefined;
    await patchExternalSessionId(config, nativeSessionId);
    await postEvent(config, {
      type: "external_session_status",
      data: { status: "idle", response_id: `pi-${Date.now()}-${++sequence}` },
    });
  });

  pi.on("agent_start", async (_event, ctx) => {
    rememberContext(ctx);
    // A brand-new agent loop must never inherit a replay window armed before it
    // began (e.g. a spuriously-armed window from an interrupt that landed while
    // idle). A legitimate interrupt that arrives after this point belongs to
    // this loop and can still arm/replay; agent_end clears once the loop
    // completes. See F18.
    clearPendingInterrupt();
    agentRunning = true;
    setOmnigentStatus(config, ctx, "running");
    activeResponseId = null;
    turnOrdinal = 0;
    postedToolCalls.clear();
    postedToolResults.clear();
    postedReasoning.clear();
    toolCallsById.clear();
    streamedTextIndex.clear();
    finalizedTextBlocks.clear();
    streamingMessageOrdinal = 0;
    await postEvent(config, {
      type: "external_session_status",
      data: {
        status: "running",
        response_id: `pi-${Date.now()}-${++sequence}`,
      },
    });
  });

  pi.on("agent_end", async (event, ctx) => {
    rememberContext(ctx);
    clearPendingInterrupt();
    agentRunning = false;
    setOmnigentStatus(config, ctx, "idle");
    activeResponseId = null;
    // Last-chance usage capture from the agent loop's final message set, in
    // case neither ``message_end`` nor ``turn_end`` carried usage for some
    // call. ``event.messages`` may hold the whole conversation; the
    // fingerprint dedup means re-scanning already-counted messages is a no-op,
    // so a plain forward-scan is safe (no overcount).
    const messages =
      event && Array.isArray(event.messages) ? event.messages : [];
    let changed = false;
    for (const message of messages) {
      if (accumulateUsage(message)) changed = true;
    }
    if (changed) await postSessionUsage();
    await postEvent(config, {
      type: "external_session_status",
      data: { status: "idle", response_id: `pi-${Date.now()}-${++sequence}` },
    });
  });

  pi.on("turn_start", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const index =
      event && typeof event.turnIndex === "number"
        ? event.turnIndex
        : turnOrdinal + 1;
    turnOrdinal = index;
    activeResponseId = newResponseId(`turn-${turnOrdinal}`);
  });

  pi.on("message_update", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    const update = event ? event.assistantMessageEvent : undefined;
    if (!update || typeof update !== "object") return;
    if (update.type === "text_delta") {
      await postTextDelta(update, responseId);
      return;
    }
    if (update.type === "toolcall_end") {
      await postToolCall(update.toolCall, responseId);
      return;
    }
    if (update.type === "thinking_end") {
      const key = `${turnOrdinal}:${update.contentIndex}`;
      await postReasoningText(update.content, responseId, key);
    }
  });

  pi.on("tool_execution_start", async (_event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
  });

  pi.on("tool_call", async (event, ctx) => {
    rememberContext(ctx);
    const blocked = replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    await postToolCall(
      {
        id: event && event.toolCallId,
        name: event && event.toolName,
        arguments: event && event.input,
      },
      responseId,
    );
    if (blocked) {
      return { block: true, reason: "Interrupted by user" };
    }
    // Bridged Omnigent tools (registered via pi.registerTool above) are
    // policy-evaluated server-side inside the /mcp proxy when execute() runs,
    // so skip the hook-level eval for them to avoid double-evaluation and, for
    // ASK policies, a double prompt. Pi's own built-in tools (read/shell/etc)
    // are NOT bridged and stay gated here.
    if (bridgedTools.has((event && event.toolName) || "")) {
      return;
    }
    // Evaluate TOOL_CALL policy via the Omnigent server's session-level HTTP
    // endpoint. This works even after the harness turn has completed (which
    // happens immediately for pi-native — just enqueue + TurnComplete), so
    // the verdict is always evaluated against live session policies regardless
    // of whether an Omnigent turn is currently in flight.
    const verdict = await evalNativePolicyHttp(config, "PHASE_TOOL_CALL", {
      name: (event && event.toolName) || "",
      arguments: (event && event.input) || {},
    });
    if (verdict && verdict.block) {
      return {
        block: true,
        reason: verdict.reason || "blocked by Omnigent policy",
      };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    rememberContext(ctx);
    const blocked = replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    // A pending interrupt short-circuits BEFORE the result-policy evaluation —
    // mirroring the tool_call gate, which returns on ``blocked`` before its own
    // (potentially long-parking) policy call. The TOOL_RESULT gate can PARK
    // server-side waiting for a human approval, and that fetch is not bound to
    // ctx.abort(); without this guard a user interrupt arriving while the gate
    // is parked would be swallowed and the agent loop held until the human
    // resolves the card or the park ceiling. The interrupt already aborts the
    // turn (replayPendingInterrupt → ctx.abort()); we just mirror the real
    // result once (so the conversation stays consistent) and return nothing so
    // we neither park nor suppress an interrupted turn's result.
    if (blocked) {
      await postToolResult(event, responseId);
      return;
    }
    // Evaluate the TOOL_RESULT policy checkpoint BEFORE mirroring the result
    // to Omnigent or returning it to the model. This is the second enforcement
    // point (SKILL.md harness checklist #6): the server gates the *output* with
    // ALLOW / ASK / DENY. ASK is parked server-side and collapses to ALLOW or
    // DENY (same machinery as the tool-call gate), so the extension only ever
    // observes a hard verdict here. PHASE_TOOL_RESULT fails OPEN, so a server
    // outage never suppresses a legitimately-produced result.
    //
    // Pi's agent loop applies this handler's returned {content, isError} to the
    // finalized tool result (agent-loop.finalizeExecutedToolCall) BEFORE it
    // builds the toolResult message the model consumes and BEFORE it emits
    // tool_execution_end — so a DENY replacement reaches the model and the
    // dedup'd mirror below reflects exactly what the model saw.
    const toolName =
      (event && event.toolName) ||
      (event && toolCallsById.get(String(event.toolCallId || ""))?.name) ||
      "";
    const verdict = await evalNativePolicyHttp(
      config,
      "PHASE_TOOL_RESULT",
      { result: textFromToolResult(event) },
      { name: toolName, arguments: (event && event.input) || {} },
    );
    if (verdict && verdict.block) {
      // DENY → suppress the real output. Replace the result content with a
      // policy-denied error the model receives in place of the tool output,
      // and mark it an error so the model treats it as a failed call. Mirror
      // the SAME suppressed text to Omnigent so the conversation matches what
      // the model consumed (postToolResult dedups on response_id:call_id, so
      // the later tool_execution_end / turn_end mirrors are no-ops).
      const text = deniedResultText(verdict.reason);
      await postToolResult(event, responseId, text);
      return { content: [{ type: "text", text }], isError: true };
    }
    // ALLOW: mirror the real result as-is and return nothing so Pi hands the
    // real output to the model unchanged.
    await postToolResult(event, responseId);
  });

  pi.on("tool_execution_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    // Backstop mirror only: the TOOL_RESULT policy verdict (incl. any DENY
    // suppression) was already applied in the ``tool_result`` handler, which
    // runs first and dedups the mirror. This event carries the
    // already-modified result, so a no-op here just covers the case where no
    // ``tool_result`` handler ran (it always does in this extension).
    await postToolResult(event, currentResponseId());
  });

  pi.on("input", async (event, ctx) => {
    rememberContext(ctx);
    setOmnigentStatus(config, ctx, "running");
    const text = event && typeof event.text === "string" ? event.text : "";
    if (!text) return;
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: `pi-user-${Date.now()}-${++sequence}`,
        item_type: "message",
        item_data: {
          role: "user",
          content: [{ type: "input_text", text }],
        },
      },
    });
  });

  pi.on("message_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    setOmnigentStatus(config, ctx, undefined);
    const message = event ? event.message : undefined;
    const role = messageRole(message);
    if (role !== "assistant") return;
    const responseId = currentResponseId();
    // Close the live preview for this message (no-op if nothing streamed),
    // then bump the ordinal so the NEXT assistant message of this turn
    // streams under a fresh, distinct id and never reuses this one's. The
    // ordinal advances here (not on message_start) so the deltas just
    // posted and this finalize agree on the id regardless of whether Pi
    // fires message_start.
    await finalizeStreamingMessage(responseId);
    streamingMessageOrdinal += 1;
    await mirrorAssistantMessage(message, responseId);
    // ``message_end`` is the primary usage-capture site (one completed
    // assistant message per LLM call); fold its token counts into the
    // cumulative session totals and flush to the server for pricing.
    if (accumulateUsage(message)) await postSessionUsage();
    const text = textFromMessage(message);
    if (!text) return;
    // The authoritative assistant item. The web UI retires + replaces the
    // oldest in-flight live preview in place with this (FIFO; one preview
    // per message), so the streamed partials never duplicate the final.
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "message",
        item_data: {
          role: "assistant",
          agent: "Pi",
          content: [{ type: "output_text", text }],
        },
      },
    });
  });

  pi.on("turn_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    await mirrorAssistantMessage(event && event.message, responseId);
    // Fallback usage capture: if Pi attached usage to the turn's final
    // assistant message but no ``message_end`` carried it, fold it in here.
    // Deduped by fingerprint, so a message already counted on ``message_end``
    // is a no-op.
    if (accumulateUsage(event && event.message)) await postSessionUsage();
    const results =
      event && Array.isArray(event.toolResults) ? event.toolResults : [];
    for (const result of results) {
      await postToolResult(result, responseId);
    }
  });
};
