// The design's history trace notation, `[F:a.ts, P:#12*]`, for tests of the
// timeline and of the tab store that keeps it.

import type { WorkspaceHistoryState } from "../workspaceHistory";
import type { BrowserEntry, Visit, WorkspacePlace } from "../workspacePlace";

/** The design's trace notation: `[F:a.ts, P:#12*]`. */
export function describeHistory(history: WorkspaceHistoryState): string {
  const parts = history.visits.map(
    (visit, index) => describeVisit(visit) + (index === history.cursor ? "*" : ""),
  );
  return `[${parts.join(", ")}]`;
}

function describeVisit(visit: Visit): string {
  const detail = describePlace(visit.place, visit.entry);
  return detail ? `${visit.tabId}:${detail}` : visit.tabId;
}

function describePlace(place: WorkspacePlace, entry: BrowserEntry | undefined): string {
  switch (place.kind) {
    case "files":
      return place.path ? place.path + (place.line ? `:${place.line.start}` : "") : "";
    case "changes":
      return place.path ?? "";
    case "pull": {
      const number = `#${place.url.split("/").at(-1)}`;
      const view = place.view === "summary" ? "" : `/${place.view}`;
      return number + view + (place.path ? `:${place.path}` : "");
    }
    case "web":
      return (place.url ? new URL(place.url).pathname : "") + (entry ? `#${entry.index}` : "");
    default:
      return "";
  }
}
