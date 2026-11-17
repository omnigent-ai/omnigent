import { createContext, type ReactNode, useContext } from "react";
import { isElectronShell } from "./nativeBridge";

const EmbeddedContext = createContext<boolean | undefined>(undefined);

export function EmbeddedProvider({
  children,
  isDarkMode = false,
}: {
  children: ReactNode;
  isDarkMode?: boolean;
}) {
  return <EmbeddedContext.Provider value={isDarkMode}>{children}</EmbeddedContext.Provider>;
}

/** True when web is rendered inside a host via `embed.tsx`. */
export function useIsEmbedded(): boolean {
  return useContext(EmbeddedContext) !== undefined;
}

/** Desktop follows the OS; browser embeds follow the host's appearance setting. */
export function useHostTheme(): "light" | "dark" | undefined {
  const isDarkMode = useContext(EmbeddedContext);
  if (isDarkMode === undefined || isElectronShell()) return undefined;
  return isDarkMode ? "dark" : "light";
}
