import { BotIcon } from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import { readSubagentActivity } from "@/lib/subagentActivity";

export function SubagentActivityMessage({ data }: { data: Record<string, unknown> }) {
  const { search } = useLocation();
  const activity = readSubagentActivity(data);
  if (!activity) return null;
  const params = new URLSearchParams(sessionNavigationSearch(search));
  params.set("panel", "agents");
  const to = {
    pathname: `/c/${encodeURIComponent(activity.sessionId)}`,
    search: `?${params.toString()}`,
  };
  let label = activity.phase === "delegated" ? "Started" : "Completed";
  if (activity.phase === "returned") {
    if (activity.status === "failed") label = "Failed";
    if (["cancelled", "killed", "stopped"].includes(activity.status ?? "")) label = "Stopped";
  }
  return (
    <div
      className="not-prose flex w-full items-center gap-2 py-2 text-sm text-muted-foreground"
      data-testid="subagent-activity"
      data-phase={activity.phase}
      data-child-session-id={activity.sessionId}
    >
      <span aria-hidden="true" className="min-w-0 flex-1 border-t border-dashed border-border" />
      <div className="flex min-w-0 max-w-[85%] items-center gap-1.5 rounded-full border border-transparent px-3 py-1 text-center transition-colors hover:border-border focus-within:border-border">
        <BotIcon aria-hidden="true" className="size-4 shrink-0" />
        <span className="min-w-0 break-words">
          {label}{" "}
          <Link
            to={to}
            className="rounded-sm font-semibold text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {activity.title}
          </Link>
        </span>
      </div>
      <span aria-hidden="true" className="min-w-0 flex-1 border-t border-dashed border-border" />
    </div>
  );
}
