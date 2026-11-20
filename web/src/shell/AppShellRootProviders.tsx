import type { ComponentProps, ReactNode } from "react";

import { CommandContextProvider } from "./commandContext";
import { FileViewerContext } from "./FileViewerContext";

type CommandContextInputs = Omit<ComponentProps<typeof CommandContextProvider>, "children">;

/**
 * The shell's root providers: the ⌘K command context around the file viewer context. One
 * element keeps AppShell's render tree at the shell layout's own indentation.
 */
export function AppShellRootProviders({
  command,
  fileViewer,
  children,
}: {
  command: CommandContextInputs;
  fileViewer: ComponentProps<typeof FileViewerContext.Provider>["value"];
  children: ReactNode;
}) {
  return (
    <CommandContextProvider
      session={command.session}
      view={command.view}
      workspaceTab={command.workspaceTab}
      filePath={command.filePath}
    >
      <FileViewerContext.Provider value={fileViewer}>{children}</FileViewerContext.Provider>
    </CommandContextProvider>
  );
}
