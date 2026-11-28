// The notices that say how each of a Browser tab's design requests went, and when a
// closed tool's last change didn't reach the page.
import { useEffect } from "react";
import { useDesignOutbox, type DesignRequest } from "./core/designOutbox";
import { onEndFailed } from "./core/toolSession";

const END_FAILED =
  "The page didn't take the tool's last change, so it may not show your latest edit.";

const OUTCOME_NOTICES: Partial<Record<DesignRequest["status"], string>> = {
  sent: "Sent to agent.",
};

/** Say when one of this tab's requests is sent, or why it wasn't (or a tool's end failed). */
export function useOutboxNotices(tabId: string, onNotice: (message: string) => void): void {
  useEffect(
    () =>
      onEndFailed((tab) => {
        if (tab === tabId) onNotice(END_FAILED);
      }),
    [tabId, onNotice],
  );
  useEffect(() => {
    const statuses = (requests: readonly DesignRequest[]) =>
      new Map(requests.filter((r) => r.tabId === tabId).map((r) => [r.id, r.status]));
    let previous = statuses(useDesignOutbox.getState().requests);
    return useDesignOutbox.subscribe(({ requests }) => {
      for (const request of requests) {
        if (request.tabId !== tabId || previous.get(request.id) === request.status) continue;
        const message = OUTCOME_NOTICES[request.status] ?? request.error;
        if (message) onNotice(message);
      }
      previous = statuses(requests);
    });
  }, [tabId, onNotice]);
}
