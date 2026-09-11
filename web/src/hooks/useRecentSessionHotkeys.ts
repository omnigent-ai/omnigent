import { useEffect, useRef } from "react";
import { getOmnigentServerIdentity } from "@/lib/host";
import { isNativeShell } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

export function useRecentSessionHotkeys(
  availableIds: readonly string[],
  activeId: string | undefined,
): void {
  const navigate = useNavigate();
  const serverIdentity = getOmnigentServerIdentity();
  const previousServer = useRef(serverIdentity);
  const history = useRef<string[]>([]);
  const knownIds = useRef<readonly string[]>([]);
  const cycle = useRef<{ ids: string[]; target: string } | null>(null);
  const latest = useRef({ availableIds, activeId });
  latest.current = { availableIds, activeId };

  useEffect(() => {
    if (previousServer.current !== serverIdentity) {
      previousServer.current = serverIdentity;
      history.current = [];
      knownIds.current = [];
      cycle.current = null;
    }
    const removed = new Set(knownIds.current.filter((id) => !availableIds.includes(id)));
    knownIds.current = availableIds;
    history.current = history.current.filter((id) => !removed.has(id));
    if (cycle.current) cycle.current.ids = cycle.current.ids.filter((id) => !removed.has(id));
    if (cycle.current && activeId === cycle.current.target) return;
    cycle.current = null;
    if (activeId && !removed.has(activeId)) {
      history.current = [activeId, ...history.current.filter((id) => id !== activeId)];
    }
  }, [availableIds, activeId, serverIdentity]);

  useEffect(() => {
    const finish = () => {
      const target = cycle.current?.target;
      if (target && cycle.current?.ids.includes(target)) {
        history.current = [target, ...history.current.filter((id) => id !== target)];
      }
      cycle.current = null;
    };
    const keydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat) return;
      if (isNativeShell() ? event.key !== "Tab" : event.code !== "Backquote") return;
      if (!event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.getModifierState("AltGraph")) return;
      const focused = document.activeElement;
      if (focused instanceof Element && focused.closest(".xterm, .monaco-editor, [role=dialog]")) {
        return;
      }
      const { activeId: active } = latest.current;
      const ids = cycle.current?.ids ?? history.current;
      if (ids.length === 0 || (ids.length === 1 && ids[0] === active)) return;
      const currentId = cycle.current?.target ?? active;
      const current = ids.indexOf(currentId ?? "");
      const index =
        current === -1
          ? event.shiftKey
            ? ids.length - 1
            : 0
          : (current + (event.shiftKey ? -1 : 1) + ids.length) % ids.length;
      const target = ids[index];
      cycle.current = { ids, target };
      event.preventDefault();
      event.stopPropagation();
      if (target !== currentId) navigate(`/c/${target}`);
    };
    const keyup = (event: KeyboardEvent) => {
      if (!event.ctrlKey) finish();
    };
    const visibilitychange = () => {
      if (document.hidden) finish();
    };
    window.addEventListener("keydown", keydown);
    window.addEventListener("keyup", keyup);
    window.addEventListener("blur", finish);
    document.addEventListener("visibilitychange", visibilitychange);
    return () => {
      window.removeEventListener("keydown", keydown);
      window.removeEventListener("keyup", keyup);
      window.removeEventListener("blur", finish);
      document.removeEventListener("visibilitychange", visibilitychange);
    };
  }, [navigate]);
}
