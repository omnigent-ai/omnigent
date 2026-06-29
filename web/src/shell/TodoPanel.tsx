import { CheckCircle2Icon, CircleIcon, CircleDotIcon } from "lucide-react";
import { useChatStore } from "@/store/chatStore";
import { cn } from "@/lib/utils";

type TodoItem = {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm: string;
};

interface TodoPanelProps {
  frameless?: boolean;
}

function TodoIcon({ status }: { status: TodoItem["status"] }) {
  if (status === "completed") {
    return <CheckCircle2Icon className="h-3 w-3 shrink-0 text-green-500" />;
  }
  if (status === "in_progress") {
    return <CircleDotIcon className="h-3 w-3 shrink-0 text-blue-500" />;
  }
  return <CircleIcon className="h-3 w-3 shrink-0 text-muted-foreground" />;
}

/**
 * Displays Claude Code's active todo list for `omnigent claude` sessions.
 *
 * Reads from `useChatStore.todos` which is populated by:
 * - the session snapshot on bind (from `_session_todos_cache` on the server)
 * - `session.todos` SSE events emitted whenever the forwarder detects a
 *   change in Claude's `~/.claude/todos/{session_id}-agent-{session_id}.json`
 *
 * Renders nothing when the todo list is empty, so the panel occupies no
 * space for sessions that have never had todos (non-claude-native, or
 * claude-native before the first turn creates any todos).
 */
export function TodoPanel({ frameless = false }: TodoPanelProps) {
  const todos = useChatStore((s) => s.todos);

  if (todos.length === 0) return null;

  return (
    <div
      className={cn(
        "flex flex-1 flex-col bg-card",
        !frameless && "border-t border-b border-border",
      )}
    >
      <ul className="overflow-y-auto px-2 py-2">
        {todos.map((todo, i) => (
          <li
            // eslint-disable-next-line react/no-array-index-key
            key={i}
            className={cn(
              "flex items-center gap-2 rounded px-1.5 py-1 text-xs",
              todo.status === "completed" && "opacity-50",
            )}
          >
            <TodoIcon status={todo.status} />
            <span className="min-w-0">
              <span
                className={cn(
                  "block break-words leading-snug",
                  todo.status === "completed" && "line-through",
                )}
              >
                {todo.content}
              </span>
              {todo.status === "in_progress" &&
                todo.activeForm &&
                todo.activeForm !== todo.content && (
                  <span className="block truncate italic text-muted-foreground">
                    {todo.activeForm}
                  </span>
                )}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
