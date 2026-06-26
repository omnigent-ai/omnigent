// Inline approval / option-picker card rendered when the server
// emits an MCP-shape `response.elicitation_request`.
//
// Render modes (decided in order):
//
//   - **ExitPlanMode plan review** — when the elicitation carries a
//     structured `exitPlanMode` payload (the PermissionRequest
//     endpoint stamps the full tool_input when the gated tool is
//     Claude's built-in ExitPlanMode). Renders the plan markdown
//     plus approve / approve-in-auto-mode / reject-with-feedback
//     actions — see `ExitPlanModeReview`.
//
//   - **AskUserQuestion form** — when the elicitation carries a
//     structured `askUserQuestion` payload (the PermissionRequest
//     endpoint stamps this when the gated tool is Claude's built-in
//     AskUserQuestion). Renders a multi-question form with radio
//     inputs for single-select, checkboxes for multi-select. Submit
//     posts the gathered answers as `content.answers`.
//
//   - **Option buttons** — when `requestedSchema` is
//     `{properties: {answer: {enum: [...]}}}`. Currently no
//     producer emits this for built-in AskUserQuestion, but the
//     branch is kept for future MCP-elicitation flows that ride
//     the same card.
//
//   - **Binary approve/reject** — everything else (policy ASK,
//     PermissionRequest for non-AskUserQuestion tools).
//
// Submit posts through `chatStore.submitApproval`, which:
//   1. optimistically flips the block to "responded" (instant UI),
//   2. calls `approve(targetSessionId, elicitationId, {action, content?})`
//      on `POST /v1/sessions/{id}/elicitations/{eid}/resolve`,
//   3. rolls back to "pending" on network error.

import {
  CheckIcon,
  ClipboardListIcon,
  ExternalLinkIcon,
  InfoIcon,
  MessageCircleQuestionMark,
  TerminalIcon,
  XIcon,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  type AskUserQuestionPayload,
  castAskUserQuestionPayload,
  parseAskUserQuestionPreview,
} from "@/lib/askUserQuestion";
import { formatPreview } from "@/lib/previewFormat";
import type { RenderItem } from "@/lib/renderItems";
import type { RememberScope } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import { AskUserQuestionForm, type AskUserQuestionAnswers } from "./AskUserQuestionForm";
import { ExitPlanModeReview } from "./ExitPlanModeReview";

/**
 * Extract the answer-option labels from an AskUserQuestion-shaped
 * ``requestedSchema``. Returns an empty array for any other schema.
 *
 * Currently unused for built-in AskUserQuestion (which routes
 * through PermissionRequest with a content_preview rather than a
 * structured schema), but kept for MCP-elicitation paths that may
 * emit this shape.
 */
function extractOptionLabels(schema: Record<string, unknown>): string[] {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object") return [];
  const answer = (properties as Record<string, unknown>).answer;
  if (!answer || typeof answer !== "object") return [];
  const enumValues = (answer as Record<string, unknown>).enum;
  if (!Array.isArray(enumValues)) return [];
  return enumValues.filter((v): v is string => typeof v === "string" && v.length > 0);
}

/**
 * Verdict submitter — same signature as `chatStore.submitApproval`.
 * Injectable so surfaces outside the active chat (the Inbox page)
 * can route the verdict to the owning session themselves.
 */
export type SubmitApprovalFn = (
  elicitationId: string,
  action: "accept" | "decline",
  content?: Record<string, unknown>,
) => void;

interface ApprovalCardProps {
  elicitationId: string;
  message: string;
  phase: string;
  policyName: string;
  contentPreview: string;
  requestedSchema: Record<string, unknown>;
  /**
   * Standalone approval page URL when the elicitation uses URL mode.
   * When present, the pending card renders a link to the approval page
   * instead of inline approve/reject buttons.
   */
  url?: string | null;
  status: "pending" | "responded";
  response: {
    action: "accept" | "decline" | "cancel" | "auto_resolved";
    content?: Record<string, unknown>;
  } | null;
  /**
   * Structured AskUserQuestion payload — set when the server-side
   * PermissionRequest endpoint detected the gated tool is
   * AskUserQuestion. Carries the FULL question + options structure
   * (not truncated like `contentPreview`). Optional/null for
   * other elicitations.
   */
  askUserQuestion?: Record<string, unknown> | null;
  /**
   * Full ExitPlanMode tool_input (untruncated) — set when the
   * server-side PermissionRequest endpoint detected the gated tool
   * is ExitPlanMode. The card renders `plan` as markdown with
   * plan-review actions. Optional/null for other elicitations.
   */
  exitPlanMode?: Record<string, unknown> | null;
  /**
   * Structured Codex command approval details. When present, the
   * card renders command metadata instead of the raw JSON preview.
   */
  codexCommand?: {
    command: string;
    cwd: string | null;
    reason: string | null;
    execPolicyAmendment: string[] | null;
  } | null;
  /**
   * Claude-native edit-tool prompts only: when true, the binary
   * approve/reject card grows a third "Accept & allow all edits"
   * button. Accepting through it asks the server to switch the
   * session into Claude Code's ``acceptEdits`` mode (the web
   * equivalent of the native shift+tab toggle). Absent/false for
   * every other elicitation, so the button never renders where the
   * mode switch would be a no-op.
   */
  allowAllEdits?: boolean;
  /**
   * Claude-native non-edit tool prompts only: when set, the binary
   * approve/reject card grows a third "Approve & don't ask again for
   * <host|tool>" button. Accepting through it asks the server to
   * install a session-scoped allow rule for the tool (scoped to
   * ``host`` for WebFetch, tool-wide otherwise) — the web equivalent
   * of Claude Code's native "don't ask again" permission option, so
   * same-scope calls stop re-prompting. Absent/null for every other
   * elicitation (edit tools take the ``allowAllEdits`` path instead).
   */
  rememberScope?: RememberScope | null;
  /**
   * Verdict submitter override. Defaults to `chatStore.submitApproval`
   * (the in-chat path: optimistic block flip + resolve POST + rollback).
   * The Inbox page passes its own handler because its cards belong to
   * sessions other than the chat store's active one.
   */
  onSubmit?: SubmitApprovalFn;
}

export function ApprovalCard({
  elicitationId,
  message,
  phase,
  policyName,
  contentPreview,
  requestedSchema,
  url,
  status,
  response,
  askUserQuestion,
  exitPlanMode,
  codexCommand,
  allowAllEdits,
  rememberScope,
  onSubmit,
}: ApprovalCardProps) {
  const submit: SubmitApprovalFn =
    onSubmit ??
    ((id, action, content) => {
      void useChatStore.getState().submitApproval(id, action, content);
    });
  const submitBinary = (action: "accept" | "decline") => {
    submit(elicitationId, action);
  };
  const submitOption = (label: string) => {
    submit(elicitationId, "accept", { answer: label });
  };
  const submitAnswers = (answers: AskUserQuestionAnswers) => {
    // ``content`` is MCP's ``ElicitResult.content``: a flat
    // ``{[field]: scalar | string[]}`` map, where each AskUserQuestion
    // question text is one "field". Wrapping in a nested ``answers``
    // object would fail server-side schema validation (content values
    // must be str / int / float / bool / list[str] / null).
    submit(elicitationId, "accept", answers);
  };
  const submitExecPolicyAmendment = (amendment: string[]) => {
    submit(elicitationId, "accept", { execpolicy_amendment: amendment });
  };
  const submitAllowAllEdits = () => {
    // Accept AND ask the server to switch the session's permission
    // mode. The server reads ``content.allow_all_edits`` and echoes a
    // ``setMode`` permission update back to the PermissionRequest
    // hook: ``acceptEdits`` for edit-tool prompts, ``auto`` when the
    // gated tool is ExitPlanMode (the plan card's "Yes, and use auto
    // mode" action — same flag, server picks the mode).
    submit(elicitationId, "accept", { allow_all_edits: true });
  };
  const submitRemember = () => {
    // Accept AND ask the server to install a session-scoped allow rule
    // so the same scope stops prompting. The server reads
    // ``content.remember`` and re-derives the rule scope (WebFetch
    // domain or tool-wide) from the gated tool itself — the client only
    // signals intent, never the rule — then echoes an ``addRules``
    // permission update back to the PermissionRequest hook.
    submit(elicitationId, "accept", { remember: true });
  };
  const submitPlanRejection = (feedback: string) => {
    // The typed feedback rides on `content.feedback`; the server
    // forwards it to Claude as the deny `message`, so Claude stays in
    // plan mode and revises toward it. Empty feedback → plain decline.
    const trimmed = feedback.trim();
    submit(elicitationId, "decline", trimmed ? { feedback: trimmed } : undefined);
  };

  // Mode detection. Prefer the server-stamped structured payload
  // (full, non-truncated); fall back to parsing the content_preview
  // JSON for backwards compatibility with elicitations published
  // before the structured field was added.
  const askPayload: AskUserQuestionPayload | null =
    castAskUserQuestionPayload(askUserQuestion) ?? parseAskUserQuestionPreview(contentPreview);
  // ExitPlanMode plan review: the server stamps the full tool_input
  // as `exit_plan_mode`; a usable plan card needs the `plan` markdown
  // string. Anything else falls back to the binary card.
  const exitPlanModePlan =
    exitPlanMode && typeof exitPlanMode.plan === "string" && exitPlanMode.plan
      ? exitPlanMode.plan
      : null;
  const isExitPlanMode = exitPlanModePlan !== null;
  const optionLabels = askPayload === null ? extractOptionLabels(requestedSchema) : [];
  const isAskUserQuestion = askPayload !== null;
  const isMultiChoice = optionLabels.length > 0;
  const isCodexCommandApproval = codexCommand !== null && codexCommand !== undefined;
  // External URL: the elicitation points to a third-party page (OAuth,
  // external MCP server, etc.) — show a link. Our own /approve/...
  // paths are handled inline with approve/reject buttons.
  const isExternalUrl = typeof url === "string" && url.length > 0 && !url.startsWith("/approve/");
  const askUserQuestionTitle =
    policyName.startsWith("agy_") || phase.startsWith("agy_")
      ? "Antigravity needs your input"
      : policyName.startsWith("codex_") || phase.startsWith("codex_")
        ? "Codex needs input"
        : policyName.startsWith("cursor_") || phase.startsWith("cursor_")
          ? "Cursor has questions"
          : "Claude has questions";

  // Hide the raw JSON preview for AskUserQuestion (the form already
  // renders the questions + options structurally) and for option-
  // button mode (the buttons render the choices). Codex command
  // approvals get a dedicated command render below, so showing the
  // transport JSON would expose unrelated ids and duplicate details.
  const formattedPreview =
    isAskUserQuestion || isExitPlanMode || isMultiChoice || isCodexCommandApproval
      ? ""
      : formatPreview(contentPreview);
  const execPolicyAmendment =
    codexCommand?.execPolicyAmendment && codexCommand.execPolicyAmendment.length > 0
      ? codexCommand.execPolicyAmendment
      : null;
  const acceptedWithExecPolicy =
    Array.isArray(response?.content?.execpolicy_amendment) &&
    response.content.execpolicy_amendment.every((entry) => typeof entry === "string");
  const acceptedAllEdits = response?.content?.allow_all_edits === true;
  const acceptedRemember = response?.content?.remember === true;
  // Persistent "don't ask again" affordance: label by the WebFetch
  // domain when present, else the tool name. Drives the third binary
  // button and the responded-state pill.
  const rememberTarget = rememberScope ? (rememberScope.host ?? rememberScope.tool) : null;
  // Tooltip spelling out the scope — the tool-wide case (no host) is a
  // broad grant (every call to the tool), so make that explicit rather
  // than letting the short button label imply a narrower scope.
  const rememberTitle = rememberScope
    ? rememberScope.host
      ? `Won't ask again for ${rememberScope.host} for the rest of this session`
      : `Won't ask again for any ${rememberScope.tool} call for the rest of this session`
    : undefined;
  const binaryButtons = (
    <div className="flex flex-wrap gap-2 pt-1">
      <Button size="sm" onClick={() => submitBinary("accept")}>
        <CheckIcon className="mr-1 size-3.5" />
        Approve
      </Button>
      {allowAllEdits && (
        <Button size="sm" variant="outline" onClick={submitAllowAllEdits}>
          <CheckIcon className="mr-1 size-3.5" />
          Accept & allow all edits
        </Button>
      )}
      {rememberTarget && (
        <Button
          size="sm"
          variant="outline"
          onClick={submitRemember}
          title={rememberTitle}
          data-testid="approval-card-remember"
        >
          <CheckIcon className="mr-1 size-3.5" />
          Approve &amp; don't ask again for {rememberTarget}
        </Button>
      )}
      <Button size="sm" variant="outline" onClick={() => submitBinary("decline")}>
        <XIcon className="mr-1 size-3.5" />
        Reject
      </Button>
    </div>
  );
  const codexCommandButtons = (
    <div className="flex flex-wrap items-center gap-2 pt-1" data-testid="codex-command-actions">
      <Button size="sm" onClick={() => submitBinary("accept")}>
        <CheckIcon className="mr-1 size-3.5" />
        Approve
      </Button>
      {execPolicyAmendment && (
        <Button
          size="sm"
          variant="outline"
          onClick={() => submitExecPolicyAmendment(execPolicyAmendment)}
        >
          <CheckIcon className="mr-1 size-3.5" />
          Approve and remember
        </Button>
      )}
      <Button size="sm" variant="outline" onClick={() => submitBinary("decline")}>
        <XIcon className="mr-1 size-3.5" />
        Reject
      </Button>
    </div>
  );

  if (status === "responded" && response) {
    const autoResolved = response.action === "auto_resolved";
    const accepted = response.action === "accept";

    // Distinguish three responded sub-states:
    //   1. AskUserQuestion submitted → "Submitted" + summary
    //   2. Single option chosen → "Selected: <label>"
    //   3. Auto-resolved / generic accept / decline
    //
    // For AskUserQuestion the ``content`` IS the answers map
    // (flat ``{[question]: answer}``) — matches MCP's
    // ElicitResult.content shape.
    const submittedAnswers =
      isAskUserQuestion && response.content && Object.keys(response.content).length > 0
        ? response.content
        : null;
    const selectedAnswer =
      !isAskUserQuestion && response.content && typeof response.content.answer === "string"
        ? (response.content.answer as string)
        : null;
    // Plan rejections can carry the feedback the user typed into the
    // card; echo it on the responded pill so the chat shows WHY the
    // plan went back for revision.
    const planRejectionFeedback =
      isExitPlanMode &&
      response.action === "decline" &&
      typeof response.content?.feedback === "string" &&
      response.content.feedback
        ? response.content.feedback
        : null;

    let icon = <XIcon className="size-4 text-destructive" />;
    let label = isExitPlanMode ? "Plan rejected" : "Rejected";
    if (autoResolved) {
      // Card was cleared by the chat store when the gated tool's
      // function_call_output arrived without a UI verdict —
      // typically because the user approved (or denied) via Claude
      // Code's TUI prompt directly. We can't know the actual
      // verdict, so render a neutral pill rather than implying an
      // accept/reject decision the UI never witnessed.
      icon = <InfoIcon className="size-4 text-muted-foreground" />;
      label = "Resolved elsewhere";
    } else if (submittedAnswers !== null) {
      icon = <CheckIcon className="size-4 text-success" />;
      label = "Submitted";
    } else if (selectedAnswer !== null) {
      icon = <CheckIcon className="size-4 text-success" />;
      label = `Selected: ${selectedAnswer}`;
    } else if (acceptedWithExecPolicy) {
      icon = <CheckIcon className="size-4 text-success" />;
      label = "Approved and remembered";
    } else if (acceptedAllEdits) {
      icon = <CheckIcon className="size-4 text-success" />;
      label = isExitPlanMode ? "Plan approved · auto mode" : "Approved · auto-accepting edits";
    } else if (acceptedRemember) {
      icon = <CheckIcon className="size-4 text-success" />;
      label = rememberTarget
        ? `Approved · won't ask again for ${rememberTarget}`
        : "Approved · won't ask again";
    } else if (accepted) {
      icon = <CheckIcon className="size-4 text-success" />;
      label = isExitPlanMode ? "Plan approved" : "Approved";
    }

    return (
      <Alert
        data-testid="approval-card"
        data-state="responded"
        className="flex flex-col gap-1 border-muted"
      >
        <AlertTitle className="flex items-center gap-2 text-sm">
          {icon}
          {label}
          {policyName && <span className="text-muted-foreground text-xs">· {policyName}</span>}
        </AlertTitle>
        <AlertDescription className="flex flex-col gap-1 text-xs">
          {isCodexCommandApproval ? (
            <>
              {codexCommand.reason && <span>{codexCommand.reason}</span>}
              <pre className="overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap">
                {codexCommand.command}
              </pre>
              {codexCommand.cwd && (
                <span>
                  <span className="text-muted-foreground">cwd: </span>
                  <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                    {codexCommand.cwd}
                  </code>
                </span>
              )}
            </>
          ) : (
            <span>{message}</span>
          )}
          {submittedAnswers !== null && (
            <ul className="flex flex-col gap-0.5 pl-3">
              {Object.entries(submittedAnswers).map(([q, ans]) => (
                <li key={q}>
                  <span className="text-muted-foreground">{q}: </span>
                  {Array.isArray(ans) ? ans.join(", ") : String(ans)}
                </li>
              ))}
            </ul>
          )}
          {planRejectionFeedback !== null && (
            <span className="italic" data-testid="plan-rejection-feedback">
              “{planRejectionFeedback}”
            </span>
          )}
        </AlertDescription>
      </Alert>
    );
  }

  // Pending state.
  return (
    <Alert
      data-testid="approval-card"
      data-state="pending"
      className="flex flex-col gap-2 py-3 px-4"
    >
      <AlertTitle className="flex items-center gap-2 text-sm">
        {isCodexCommandApproval ? (
          <TerminalIcon className="size-4 text-yellow-600 dark:text-yellow-400" />
        ) : isExitPlanMode ? (
          <ClipboardListIcon className="size-4 text-yellow-600 dark:text-yellow-400" />
        ) : (
          <MessageCircleQuestionMark className="size-4 text-yellow-600 dark:text-yellow-400" />
        )}
        {isCodexCommandApproval
          ? "Command approval"
          : isExitPlanMode
            ? "Plan review"
            : isAskUserQuestion
              ? askUserQuestionTitle
              : isMultiChoice
                ? "Choose an option"
                : "Approval required"}
        {policyName && !isAskUserQuestion && !isExitPlanMode && (
          <span className="text-muted-foreground text-xs">· {policyName}</span>
        )}
        {phase && !isMultiChoice && !isAskUserQuestion && !isExitPlanMode && (
          <span className="text-muted-foreground text-xs">({phase})</span>
        )}
      </AlertTitle>
      <AlertDescription className="flex flex-col gap-2">
        {isExitPlanMode ? (
          <>
            <span>Claude finished planning and wants to proceed.</span>
            <ExitPlanModeReview
              plan={exitPlanModePlan}
              onAcceptAuto={submitAllowAllEdits}
              onAccept={() => submitBinary("accept")}
              onReject={submitPlanRejection}
            />
          </>
        ) : isAskUserQuestion ? (
          <AskUserQuestionForm
            questions={askPayload.questions}
            onSubmit={submitAnswers}
            onReject={() => submitBinary("decline")}
          />
        ) : isCodexCommandApproval ? (
          <>
            <span>Codex wants to run this command.</span>
            {codexCommand.reason && <span className="text-foreground">{codexCommand.reason}</span>}
            <pre className="overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs text-foreground whitespace-pre-wrap">
              {codexCommand.command}
            </pre>
            {codexCommand.cwd && (
              <span className="text-xs">
                cwd:{" "}
                <code className="rounded bg-muted px-1 py-0.5 font-mono">{codexCommand.cwd}</code>
              </span>
            )}
            {codexCommandButtons}
          </>
        ) : (
          <>
            <span>{message}</span>
            {formattedPreview && (
              <pre className="max-h-64 overflow-y-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap break-words">
                {formattedPreview}
              </pre>
            )}
            {isExternalUrl ? (
              <div className="flex flex-wrap gap-2 pt-1">
                <Button size="sm" asChild>
                  <a href={url!} target="_blank" rel="noopener noreferrer">
                    <ExternalLinkIcon className="mr-1 size-3.5" />
                    Open approval page
                  </a>
                </Button>
              </div>
            ) : isMultiChoice ? (
              <div className="flex flex-wrap gap-2 pt-1" data-testid="approval-card-options">
                {optionLabels.map((optLabel) => (
                  <Button
                    key={optLabel}
                    size="sm"
                    variant="outline"
                    onClick={() => submitOption(optLabel)}
                  >
                    {optLabel}
                  </Button>
                ))}
              </div>
            ) : (
              binaryButtons
            )}
          </>
        )}
      </AlertDescription>
    </Alert>
  );
}

/**
 * Render an elicitation ``RenderItem`` as an ``ApprovalCard``. The prop
 * mapping lives here, in one place, so the two callers stay in sync:
 * ``BlockRenderer`` (inline in the message stream) and ``ChatPage``'s
 * pinned tray (pending cards lifted above the composer). Pass ``onSubmit``
 * to route the verdict somewhere other than the active chat store.
 */
export function ElicitationCard({
  item,
  onSubmit,
}: {
  item: Extract<RenderItem, { kind: "elicitation" }>;
  onSubmit?: SubmitApprovalFn;
}) {
  return (
    <ApprovalCard
      elicitationId={item.elicitationId}
      message={item.message}
      phase={item.phase}
      policyName={item.policyName}
      contentPreview={item.contentPreview}
      requestedSchema={item.requestedSchema}
      url={item.url}
      status={item.status}
      response={item.response}
      askUserQuestion={item.askUserQuestion}
      exitPlanMode={item.exitPlanMode}
      codexCommand={item.codexCommand}
      allowAllEdits={item.allowAllEdits}
      rememberScope={item.rememberScope}
      onSubmit={onSubmit}
    />
  );
}
