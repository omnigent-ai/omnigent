import { ChevronDownIcon, ListTodoIcon } from "lucide-react";
import { useChatStore } from "@/store/chatStore";
import { cn } from "@/lib/utils";
import { TodoPanel } from "./TodoPanel";

/**
 * Collapsible "Plan (N/M)" tracker pinned to the top of the chat thread.
 *
 * A bounded, centered card (not a full-width pane) with an accent-tinted
 * header bar: the always-visible summary shows completed/total, and expanding
 * reveals the same task list as the workspace Tasks tab (TodoPanel). A flex
 * sibling of the conversation viewport (not an overlay) so it shrinks the
 * scroll area rather than covering messages. Self-hides when the session has
 * no tasks, matching the Tasks tab's `todosSupported` gate.
 */
export function ChatPlanAccordion({ className }: { className?: string }) {
  const todos = useChatStore((s) => s.todos);
  if (todos.length === 0) return null;
  const completed = todos.filter((t) => t.status === "completed").length;

  return (
    // Full-width layout row with side gutters; the card centers on the chat
    // column so it lines up with the message thread below.
    // chat-plan-accordion is the stylesheet hook: index.css folds the native
    // shells' safe-area header shift into this row's top margin.
    <div className={cn("chat-plan-accordion shrink-0 px-3 md:px-4", className)}>
      <details
        data-testid="plan-tracker"
        className="group mx-auto max-w-3xl overflow-hidden rounded-xl border border-accent-foreground/20 bg-card shadow-sm mb-1"
      >
        <summary className="flex cursor-pointer list-none select-none items-center gap-2 bg-accent px-3 py-2 text-ui font-medium text-accent-foreground [&::-webkit-details-marker]:hidden">
          <ListTodoIcon className="size-4 shrink-0" />
          <span>Plan</span>
          <span
            className="opacity-70"
            aria-label={`${completed} of ${todos.length} tasks completed`}
          >
            ({completed}/{todos.length})
          </span>
          <ChevronDownIcon className="ml-auto size-4 shrink-0 transition-transform group-open:rotate-180" />
        </summary>
        {/* Cap the expanded list at 150px so a long plan can't swallow the
            chat; it scrolls internally past the cap. */}
        <div className="max-h-[150px] overflow-y-auto border-t border-border">
          <TodoPanel frameless />
        </div>
      </details>
    </div>
  );
}
