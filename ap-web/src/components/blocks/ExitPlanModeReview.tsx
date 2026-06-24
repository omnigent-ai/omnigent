// Plan-review body rendered inside `ApprovalCard` when the gated tool
// is Claude Code's built-in ExitPlanMode (claude-native sessions).
//
// Mirrors Claude's native plan-approval dialog:
//
//   1. "Yes, and use auto mode" — approve the plan AND switch the
//      session into `auto` mode (the server echoes a `setMode`
//      permission update on the verdict), so the plan executes without
//      per-tool prompts.
//   2. "Yes, manually approve edits" — approve the plan; the server
//      pins the session to the prompting `default` mode so every
//      edit prompts.
//   3. "Reject with feedback" — reveals a textarea; the typed feedback
//      rides on `content.feedback` and the server forwards it to
//      Claude as the deny `message`, so Claude stays in plan mode and
//      revises toward it.
//
// The plan markdown renders through the same secure Streamdown stack
// as assistant chat bubbles (`MessageResponse`).

import { CheckIcon, XIcon, ZapIcon } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

interface ExitPlanModeReviewProps {
  /** Plan markdown from the ExitPlanMode tool_input, e.g. `"# Plan\n…"`. */
  plan: string;
  /** Approve AND switch the session into Claude's `auto` mode. */
  onAcceptAuto: () => void;
  /** Approve; the session is pinned to `default` mode (prompts per edit). */
  onAccept: () => void;
  /** Reject; `feedback` is the user's typed revision guidance (`""` when none). */
  onReject: (feedback: string) => void;
}

export function ExitPlanModeReview({
  plan,
  onAcceptAuto,
  onAccept,
  onReject,
}: ExitPlanModeReviewProps) {
  const { t } = useTranslation("common");
  const [rejecting, setRejecting] = useState(false);
  const [feedback, setFeedback] = useState("");

  return (
    <div className="flex flex-col gap-2" data-testid="exit-plan-mode-review">
      {/* text-foreground: the plan body is the card's primary content, so it
          renders in normal text color like a regular assistant message —
          escaping AlertDescription's muted default (which otherwise washes
          the whole plan out). The short lead-in caption above stays muted
          for hierarchy, matching the Codex command card. */}
      <div className="text-sm text-foreground">
        <MessageResponse>{plan}</MessageResponse>
      </div>
      {rejecting ? (
        <div className="flex flex-col gap-2 pt-1" data-testid="exit-plan-mode-feedback">
          <Textarea
            autoFocus
            placeholder={t("planFeedbackPlaceholder")}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            className="min-h-20 text-sm"
          />
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => onReject(feedback)}>
              <XIcon className="mr-1 size-3.5" />
              {t("rejectPlan")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(false)}>
              {t("cancel")}
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2 pt-1">
          <Button size="sm" onClick={onAcceptAuto}>
            <ZapIcon className="mr-1 size-3.5" />
            {t("planApproveAutoMode")}
          </Button>
          <Button size="sm" variant="outline" onClick={onAccept}>
            <CheckIcon className="mr-1 size-3.5" />
            {t("planApproveManual")}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setRejecting(true)}>
            <XIcon className="mr-1 size-3.5" />
            {t("planRejectWithFeedback")}
          </Button>
        </div>
      )}
    </div>
  );
}
