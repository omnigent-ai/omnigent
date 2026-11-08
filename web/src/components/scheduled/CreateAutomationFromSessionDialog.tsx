import { lazy, Suspense, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Conversation } from "@/hooks/useConversations";
import { showToast } from "@/components/ui/toast";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useNavigate } from "@/lib/routing";
import {
  automationPagePath,
  automationSourceFromConversation,
} from "@/lib/automationFromSession";

// Loaded on first open so the session menus don't pull the automation form into
// the main bundle (the automation pages load it lazily too).
const CreateScheduledTaskDialog = lazy(() =>
  import("./CreateScheduledTaskDialog").then((module) => ({
    default: module.CreateScheduledTaskDialog,
  })),
);

/**
 * The session menu's "Create automation" dialog: a New automation form that
 * starts every run from a copy of *conversation*. After saving, it opens the
 * new automation.
 */
export function CreateAutomationFromSessionDialog({
  conversation,
  open,
  onOpenChange,
}: {
  conversation: Conversation;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const info = useServerInfo();
  const sourceSession = useMemo(
    () => automationSourceFromConversation(conversation),
    [conversation],
  );
  return (
    <Suspense fallback={null}>
      <CreateScheduledTaskDialog
        open={open}
        onOpenChange={onOpenChange}
        sourceSession={sourceSession}
        onCreated={(task) => {
          // The copy lands in the automation's project, which each sidebar folder
          // lists on its own; refresh them so it shows without a reload.
          void queryClient.invalidateQueries({ queryKey: ["conversations"] });
          void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
          void queryClient.invalidateQueries({ queryKey: ["projects"] });
          showToast("Automation created");
          navigate(automationPagePath(info, task));
        }}
      />
    </Suspense>
  );
}
