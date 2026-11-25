// A split pane with no terminal to show: a shell that didn't start keeps its slot here, saying
// why, with Try Again and Close Pane; any other missing terminal shows the view's message.

import { type ReactNode, useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import type { TerminalInfo } from "@/hooks/useTerminals";

import { failedPaneMayTakeFocus } from "./paneShellStart";
import { type PaneStartFailure, usePaneStartFailure } from "./paneStartFailures";
import { useTerminalPaneActions } from "./terminalPaneActions";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

export function MissingTerminalPane({
  target,
  paneKey,
  focused,
  terminals,
  readOnly,
  fallback,
}: {
  target: TerminalSplitTarget;
  paneKey: string;
  /** The split's focused pane, which a failed start's Try Again then takes the keyboard for. */
  focused: boolean;
  terminals: readonly TerminalInfo[];
  readOnly: boolean;
  /** Shown when the pane isn't a failed start, e.g. "Terminal unavailable." */
  fallback: ReactNode;
}) {
  const failure = usePaneStartFailure(target.conversationId, paneKey);
  if (!failure) {
    return (
      <div className="flex flex-1 items-center justify-center px-4 text-center text-muted-foreground text-ui">
        {fallback}
      </div>
    );
  }
  return (
    <FailedPaneStart
      target={target}
      paneKey={paneKey}
      focused={focused}
      terminals={terminals}
      readOnly={readOnly}
      failure={failure}
    />
  );
}

function FailedPaneStart({
  target,
  paneKey,
  focused,
  terminals,
  readOnly,
  failure,
}: {
  target: TerminalSplitTarget;
  paneKey: string;
  focused: boolean;
  terminals: readonly TerminalInfo[];
  readOnly: boolean;
  failure: PaneStartFailure;
}) {
  const actions = useTerminalPaneActions(target, terminals);
  const canClose = !readOnly && actions.canClosePane(paneKey);
  // Focused as the pane the person was waiting for, or moved to, the keyboard lands on Try Again,
  // unless they've since gone somewhere else.
  const tryAgainRef = useRef<HTMLButtonElement>(null);
  // Try Again is disabled while another shell starts, so it takes the keyboard once that ends.
  const tryAgainEnabled = !actions.splitting;
  useEffect(() => {
    const tryAgain = tryAgainRef.current;
    if (focused && tryAgainEnabled && tryAgain && failedPaneMayTakeFocus(tryAgain)) {
      tryAgain.focus({ preventScroll: true });
    }
  }, [focused, tryAgainEnabled]);
  return (
    <div
      data-testid="terminal-start-failed"
      className="flex flex-1 flex-col items-center justify-center gap-1 px-4 text-center"
    >
      <div role="alert" className="flex flex-col gap-1">
        <p className="text-ui font-medium text-foreground">Couldn't start the shell.</p>
        <p className="text-sm text-muted-foreground">{failure.reason}</p>
      </div>
      {!readOnly && (
        <div className="mt-2 flex gap-2">
          <Button
            ref={tryAgainRef}
            type="button"
            size="xs"
            // Another shell starting in the session takes the one launch at a time.
            disabled={actions.splitting}
            onClick={() => actions.retryPaneStart(paneKey)}
            componentId="terminal.split.start_failed.retry"
          >
            Try Again
          </Button>
          {canClose && (
            <Button
              type="button"
              size="xs"
              variant="outline"
              onClick={() => actions.closePane(paneKey)}
              componentId="terminal.split.start_failed.close"
            >
              Close Pane
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
