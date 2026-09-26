import { Button } from "@/components/ui/button";
import { useWebUpdateNotifications } from "@/hooks/useWebUpdateNotifications";

export function WebUpdateBanner() {
  const { availableBuildId, dismiss } = useWebUpdateNotifications();
  if (!availableBuildId) return null;

  return (
    <div
      role="status"
      aria-label="Web app update"
      className="fixed inset-x-3 bottom-[max(0.75rem,env(safe-area-inset-bottom))] z-50 flex items-center gap-3 rounded-lg border bg-popover p-3 text-popover-foreground shadow-lg sm:inset-x-auto sm:right-4"
    >
      <span className="flex-1 text-sm font-medium">Update available</span>
      <Button size="sm" onClick={() => window.location.reload()}>
        Reload
      </Button>
      <Button size="sm" variant="ghost" onClick={dismiss}>
        Later
      </Button>
    </div>
  );
}
