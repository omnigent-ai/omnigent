// A pane whose ⌘K commands register under a namespace of its own, offered only while
// `enabled`. A scope inside a disabled one stays disabled. The pane's own shortcuts follow
// the same owner, through `useOwnsPaneCommands`.

import { type ReactNode, useContext, useId, useMemo } from "react";

import { PaletteCommandScopeContext } from "./paletteCommands";

export function PaletteCommandScope({
  enabled,
  children,
}: {
  enabled: boolean;
  children: ReactNode;
}) {
  const parent = useContext(PaletteCommandScopeContext);
  const id = useId();
  const namespace = parent ? `${parent.namespace}/${id}` : id;
  const effective = enabled && (parent?.enabled ?? true);
  const value = useMemo(() => ({ namespace, enabled: effective }), [namespace, effective]);
  return (
    <PaletteCommandScopeContext.Provider value={value}>
      {children}
    </PaletteCommandScopeContext.Provider>
  );
}

/** Whether this pane owns its commands now: the focused one of several, or the only one. */
export function useOwnsPaneCommands(): boolean {
  return useContext(PaletteCommandScopeContext)?.enabled ?? true;
}
