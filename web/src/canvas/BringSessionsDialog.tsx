// Brings existing sessions onto a project's board by moving them into the project.

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useMoveToProject, type Conversation } from "@/hooks/useConversations";
import { conversationDisplayLabel } from "@/shell/sidebarNav";

export function BringSessionsDialog({
  open,
  projectName,
  sessions,
  onClose,
}: {
  open: boolean;
  projectName: string;
  /** Sessions currently on other boards. */
  sessions: readonly Conversation[];
  onClose: () => void;
}) {
  const moveToProject = useMoveToProject();
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  useEffect(() => {
    if (open) setSelected(new Set());
  }, [open]);

  const toggle = (id: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const confirm = () => {
    for (const id of selected) moveToProject.mutate({ id, project: projectName });
    onClose();
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add sessions to {projectName}</DialogTitle>
          <DialogDescription>
            Moved sessions join this project&apos;s team, and agents in other projects can no longer
            reach them.
          </DialogDescription>
        </DialogHeader>
        {sessions.length === 0 ? (
          <p className="text-ui text-muted-foreground">No other sessions to add.</p>
        ) : (
          <ul className="flex max-h-72 flex-col gap-1 overflow-y-auto">
            {sessions.map((session) => (
              <li key={session.id}>
                <label className="flex min-w-0 items-center gap-2 rounded-md px-1 py-1 text-ui hover:bg-muted">
                  <Checkbox
                    checked={selected.has(session.id)}
                    onCheckedChange={(checked) => toggle(session.id, checked === true)}
                  />
                  <span className="truncate">{conversationDisplayLabel(session)}</span>
                </label>
              </li>
            ))}
          </ul>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" disabled={selected.size === 0} onClick={confirm}>
            Add sessions
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
