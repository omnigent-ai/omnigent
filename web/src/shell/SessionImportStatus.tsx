import type { MouseEvent } from "react";
import { Spinner } from "@/components/ui/spinner";
import { Link } from "@/lib/routing";
import { useSessionImportStore } from "@/store/sessionImport";

/** Compact import progress beside the session-list heading. */
export function SessionImportStatus({
  onNavigate,
}: {
  onNavigate?: (e: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const importing = useSessionImportStore((s) => s.importing);
  const count = useSessionImportStore((s) => s.importedCount);
  if (!importing) return null;
  return (
    <span
      role="status"
      data-testid="sidebar-import-progress"
      className="flex min-w-0 items-center gap-1 text-xs text-primary"
    >
      <Spinner className="size-3.5 shrink-0" />
      <Link
        to="/settings/import"
        onClick={onNavigate}
        className="truncate hover:underline"
        aria-label={`Importing sessions, ${count} imported. View import progress`}
        title={`Importing sessions, ${count} imported`}
        componentId="sidebar.import_progress"
      >
        Importing ({count})
      </Link>
    </span>
  );
}
