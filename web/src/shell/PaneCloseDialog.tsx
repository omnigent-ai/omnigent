// The confirmation a split's Close Pane asks before it ends a pane's shell, as closing a shell's
// tab asks; a split's hosts render one each, and the one close it asks about is shared.

import { terminalDisplayLabel } from "@/hooks/useTerminals";

import { CloseShellDialog } from "./CloseShellDialog";
import type { TerminalPaneActions } from "./terminalPaneActions";

export function PaneCloseDialog({
  actions,
}: {
  actions: Pick<TerminalPaneActions, "pendingClose" | "confirmClose" | "cancelClose">;
}) {
  const { pendingClose } = actions;
  return (
    <CloseShellDialog
      open={pendingClose !== null}
      shellLabel={pendingClose && terminalDisplayLabel(pendingClose.terminal)}
      onConfirm={actions.confirmClose}
      onCancel={actions.cancelClose}
    />
  );
}
