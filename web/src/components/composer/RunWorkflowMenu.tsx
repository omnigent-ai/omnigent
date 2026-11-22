import { type ReactNode, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { offersSavedWorkflows, WorkflowPicker } from "@/components/composer/WorkflowPicker";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { type SavedWorkflow, useSavedWorkflows } from "@/hooks/useSavedWorkflows";

/**
 * The "+" menu's Run a workflow for other surfaces, e.g. the Tasks tab. A choice
 * from the menu runs once the menu has closed, so the dialog it opens keeps focus.
 */
export function RunWorkflowMenu({
  className,
  children,
  onStart,
}: {
  className?: string;
  children: ReactNode;
  /** Opens the workflow dialog, to describe one from `description` or to run `savedWorkflow`. */
  onStart: (description?: string, savedWorkflow?: SavedWorkflow) => void;
}) {
  const { workflows, pagination, refresh, reload } = useSavedWorkflows({ includeShared: true });
  const listed = offersSavedWorkflows(workflows, pagination);
  const [open, setOpen] = useState(false);
  // Set when a reload finds workflows to run: the menu opens once the list shows them.
  const [openWhenListed, setOpenWhenListed] = useState(false);
  if (openWhenListed && listed) {
    setOpenWhenListed(false);
    setOpen(true);
  }
  const checkingRef = useRef(false);
  // A reload that ends after the button has gone, e.g. once another session shows, opens nothing.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const pendingActionRef = useRef<(() => void) | null>(null);
  const show = async () => {
    if (listed) {
      // A workflow saved since the list last loaded shows up.
      refresh();
      setOpen(true);
      return;
    }
    // An empty list may be out of date or still loading, e.g. after a workflow was
    // saved elsewhere, so reload it before going straight to describing one.
    if (checkingRef.current) return;
    checkingRef.current = true;
    try {
      const latest = await reload();
      if (!mountedRef.current) return;
      if (offersSavedWorkflows(latest.workflows, latest)) {
        setOpenWhenListed(true);
        return;
      }
      if (latest.failed) toast.error("Couldn't load your saved workflows.");
      onStart();
    } finally {
      checkingRef.current = false;
    }
  };
  return (
    <DropdownMenu
      open={open}
      onOpenChange={(next) => {
        if (next) void show();
        else setOpen(false);
      }}
    >
      <DropdownMenuTrigger asChild>
        {/* One button whether or not there's a list, so it keeps focus as the list loads. */}
        <button type="button" className={className} aria-haspopup={listed ? "menu" : "dialog"}>
          {children}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        onCloseAutoFocus={() => {
          const action = pendingActionRef.current;
          pendingActionRef.current = null;
          if (action !== null) requestAnimationFrame(action);
        }}
        collisionPadding={12}
        aria-label="Run a workflow"
        // The "+" menu's look, so the list reads the same in both places.
        className="composer-add-menu w-80 max-w-[calc(100vw-24px)] rounded-[16px] max-md:p-1.5"
      >
        <WorkflowPicker
          workflows={workflows}
          pagination={pagination}
          onSelect={(workflow) => {
            pendingActionRef.current = () => onStart("", workflow);
          }}
          onDescribe={(description) => {
            pendingActionRef.current = () => onStart(description);
          }}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
