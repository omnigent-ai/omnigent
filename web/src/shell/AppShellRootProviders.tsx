import type { ComponentProps, ReactNode } from "react";

import { CommandContextProvider } from "./commandContext";
import { GoogleDocsSessionBoundary } from "./GoogleDocsSessionContext";
import { FileViewerContext } from "./FileViewerContext";

type CommandContextInputs = Omit<ComponentProps<typeof CommandContextProvider>, "children">;

/** The shell's root providers: the ⌘K command context around the file viewer context. */
export function AppShellRootProviders({
  command,
  fileViewer,
  revealGoogleDocs,
  children,
}: {
  command: CommandContextInputs;
  fileViewer: ComponentProps<typeof FileViewerContext.Provider>["value"];
  revealGoogleDocs?: (isCurrent: () => boolean) => void;
  children: ReactNode;
}) {
  return (
    <CommandContextProvider
      session={command.session}
      view={command.view}
      workspaceTab={command.workspaceTab}
      filePath={command.filePath}
    >
      <FileViewerContext.Provider value={fileViewer}>
        <GoogleDocsSessionBoundary
          sessionId={command.session?.id}
          onReveal={revealGoogleDocs}
          permissionLevel={
            command.session?.permissionResolved ? command.session.permissionLevel : 1
          }
        >
          {children}
        </GoogleDocsSessionBoundary>
      </FileViewerContext.Provider>
    </CommandContextProvider>
  );
}
