import { useNavigate } from "@/lib/routing";
import { HANDLED, NOT_HANDLED, useRegisterAction } from "@/actions";

export function sessionTarget(
  orderedIds: readonly string[],
  activeId: string | undefined,
  direction: 1 | -1,
): string | undefined {
  if (orderedIds.length === 0) return undefined;
  const current = activeId ? orderedIds.indexOf(activeId) : -1;
  const next =
    current === -1
      ? direction === 1
        ? 0
        : orderedIds.length - 1
      : (current + direction + orderedIds.length) % orderedIds.length;
  return orderedIds[next];
}

/** Register session navigation actions against the sidebar's live render order. */
export function useSessionActions(
  orderedIds: readonly string[],
  pinnedIds: readonly string[],
  activeId: string | undefined,
): void {
  const navigate = useNavigate();
  const navigateInDirection = (direction: 1 | -1) => {
    const target = sessionTarget(orderedIds, activeId, direction);
    if (target && target !== activeId) navigate(`/c/${target}`);
  };
  const version = `${activeId ?? ""}:${orderedIds.join(",")}:${pinnedIds.join(",")}`;

  useRegisterAction(
    "session.action.openPrevious",
    {
      acceptsKeybindings: true,
      isEnabled: () => orderedIds.length > 0,
      run: () => {
        navigateInDirection(-1);
        return HANDLED;
      },
    },
    version,
  );
  useRegisterAction(
    "session.action.openNext",
    {
      acceptsKeybindings: true,
      isEnabled: () => orderedIds.length > 0,
      run: () => {
        navigateInDirection(1);
        return HANDLED;
      },
    },
    version,
  );
  useRegisterAction(
    "session.action.openPinned",
    {
      acceptsKeybindings: true,
      run: ({ args }) => {
        const target = pinnedIds[args.slot];
        if (!target) return NOT_HANDLED;
        if (target !== activeId) navigate(`/c/${target}`);
        return HANDLED;
      },
    },
    version,
  );
}
