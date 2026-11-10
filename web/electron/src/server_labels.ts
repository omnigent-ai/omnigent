/**
 * Server labels, persisted as settings.server_labels: workspace origin → the
 * server URL the user picked when Databricks sign-in moved to that workspace's
 * own host. Recents and the saved server keep the workspace URL, where the
 * sign-in is stored; a label names it for display and, via labeledWorkspace,
 * lets a later Connect to the same pick find that sign-in.
 */
export type ServerLabels = Record<string, string>;

function originOf(url: unknown): string | null {
  try {
    return new URL(String(url)).origin;
  } catch {
    return null;
  }
}

/**
 * The valid entries of a settings.server_labels value (hand-edited settings
 * may hold anything).
 */
export function parseServerLabels(value: unknown): ServerLabels {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(
      (entry): entry is [string, string] =>
        originOf(entry[0]) === entry[0] &&
        typeof entry[1] === "string" &&
        originOf(entry[1]) !== null,
    ),
  );
}

/**
 * The URL the user picked for `url`'s host, else null.
 *
 * @param labels From parseServerLabels.
 */
export function serverLabel(labels: ServerLabels, url: unknown): string | null {
  const origin = originOf(url);
  return origin !== null && Object.hasOwn(labels, origin) ? labels[origin]! : null;
}

/**
 * The workspace origin a Connect to the account URL `url` reached before, else
 * null. Only a pick naming its workspace (`?o=<workspace id>`) matches: without
 * one, the account flow asks which workspace this window should open.
 *
 * @param labels From parseServerLabels.
 */
export function labeledWorkspace(labels: ServerLabels, url: unknown): string | null {
  let entered: URL;
  try {
    entered = new URL(String(url));
  } catch {
    return null;
  }
  const workspaceId = entered.searchParams.get("o");
  if (!workspaceId) return null;
  for (const [origin, picked] of Object.entries(labels)) {
    const pick = new URL(picked);
    if (
      origin !== entered.origin &&
      pick.origin === entered.origin &&
      pick.searchParams.get("o") === workspaceId
    ) {
      return origin;
    }
  }
  return null;
}

/**
 * Labels after a connect to `picked` landed on `connected`, kept only for hosts
 * still in `recents`. Sign-in that moved hosts labels the connected host with
 * the pick; a direct connect to a host drops its label.
 *
 * @param labels From parseServerLabels.
 */
export function withConnectLabel(
  labels: ServerLabels,
  picked: string,
  connected: string,
  recents: readonly unknown[],
): ServerLabels {
  const origin = originOf(connected);
  const listed = new Set(recents.map(originOf));
  const next: ServerLabels = Object.fromEntries(
    Object.entries(labels).filter(([host]) => host !== origin && listed.has(host)),
  );
  if (origin !== null && listed.has(origin) && originOf(picked) !== origin) next[origin] = picked;
  return next;
}
