// Readable chat item for a harness-internal teammate's delivery
// (today: Claude Code agent teams). The bridge unwraps the raw
// `<teammate-message>` markup into structured data; this card shows
// who spoke (name + optional one-line summary) and the prose body —
// the machine-side idle_notification twin never reaches the renderer.

import { UsersIcon } from "lucide-react";

// Claude Code assigns each teammate a named accent color. Map the
// known names onto theme-safe tints; unknown names fall back to muted.
const COLOR_CLASSES: Record<string, string> = {
  red: "text-red-500",
  green: "text-green-600 dark:text-green-500",
  blue: "text-blue-500",
  yellow: "text-yellow-600 dark:text-yellow-500",
  purple: "text-purple-500",
  orange: "text-orange-500",
  pink: "text-pink-500",
  cyan: "text-cyan-600 dark:text-cyan-500",
};

interface TeammateMessageCardProps {
  /** The teammate's name, e.g. `buddy`. */
  teammateId: string;
  /** Prose body of the delivery. */
  text: string;
  /** One-line summary attribute, or null. */
  summary: string | null;
  /** Teammate accent color name, or null. */
  color: string | null;
}

export function TeammateMessageCard({
  teammateId,
  text,
  summary,
  color,
}: TeammateMessageCardProps) {
  const iconClass = (color && COLOR_CLASSES[color]) || "text-muted-foreground";
  return (
    <div
      data-testid="teammate-message-card"
      data-teammate-id={teammateId}
      className="not-prose w-full rounded-md border bg-card px-3 py-2"
    >
      <div className="flex items-center gap-1.5 text-sm">
        <UsersIcon className={`size-3.5 shrink-0 ${iconClass}`} />
        <span className="font-semibold">{teammateId}</span>
        <span className="text-muted-foreground">Teammate</span>
        {summary && <span className="min-w-0 truncate text-muted-foreground">— {summary}</span>}
      </div>
      {text && <p className="mt-1 whitespace-pre-wrap text-sm">{text}</p>}
    </div>
  );
}
