// What a draft or auto-merge change came to, with its Check Again, Open on GitHub and Dismiss:
// the PR page's notice, and the merge bar's own outside a page, render it alike.

import { RefreshCwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

import {
  type LifecycleNotice,
  type LifecycleNoticeAction,
  lifecycleNoticeActionLabel,
  lifecycleNoticeActions,
  lifecycleNoticeMessage,
} from "./pullRequestLifecycleActions";

export function LifecycleNoticeView({
  notice,
  prUrl,
  checking,
  checkFailed,
  onAction,
  className,
}: {
  notice: LifecycleNotice;
  prUrl: string;
  checking: boolean;
  checkFailed: boolean;
  onAction: (action: LifecycleNoticeAction) => void;
  className?: string;
}) {
  const unknown = notice.outcome.kind === "unknown";
  return (
    <div
      role={unknown ? "alert" : "status"}
      className={cn("flex flex-wrap items-center gap-2 text-ui", className)}
    >
      <p className="min-w-0 flex-1">
        {lifecycleNoticeMessage(notice)}
        {unknown && checkFailed && " It couldn’t be read just now."}
      </p>
      {lifecycleNoticeActions(notice).map((action) =>
        action === "check" ? (
          <Button
            key={action}
            size="sm"
            variant="outline"
            disabled={checking}
            onClick={() => onAction(action)}
          >
            {checking ? <Spinner aria-hidden /> : <RefreshCwIcon aria-hidden />}
            {lifecycleNoticeActionLabel(notice, action, false)}
          </Button>
        ) : action === "open" ? (
          <Button key={action} asChild size="sm" variant="ghost">
            <a href={prUrl} target="_blank" rel="noreferrer">
              {lifecycleNoticeActionLabel(notice, action, false)}
            </a>
          </Button>
        ) : (
          <Button key={action} size="sm" variant="ghost" onClick={() => onAction(action)}>
            {lifecycleNoticeActionLabel(notice, action, false)}
          </Button>
        ),
      )}
    </div>
  );
}
