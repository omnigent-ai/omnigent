import path from "node:path";

/** An IPC event from a page whose sender has a stable id. */
export interface FolderChooserEvent {
  sender: { id: number };
}

/** The OS folder chooser's options, as this module passes them. */
export interface FolderChooserDialogOptions {
  properties: ("openDirectory" | "createDirectory")[];
  defaultPath: string | undefined;
}

export interface FolderChooserDeps<Event extends FolderChooserEvent, Window> {
  ipcMain: {
    handle: (channel: string, listener: (event: Event, ...args: unknown[]) => unknown) => void;
  };
  dialog: {
    showOpenDialog: (
      window: Window,
      options: FolderChooserDialogOptions,
    ) => Promise<{ canceled: boolean; filePaths: string[] }>;
  };
  windowFor: (event: Event) => Window;
  isPinnedOriginSender: (event: Event) => boolean;
}

/**
 * Folder picking for workspaces on this machine, answered only to the pinned
 * server page: the OS folder chooser, and the local path of a file dropped
 * from the file manager (which Chromium otherwise hides from web pages).
 */
export function registerFolderChooser<Event extends FolderChooserEvent, Window>({
  ipcMain,
  dialog,
  windowFor,
  isPinnedOriginSender,
}: FolderChooserDeps<Event, Window>): void {
  // Windows with a chooser already up; a second click must not queue another.
  const choosing = new Set<number>();

  ipcMain.handle("omnigent:choose-directory", async (event, defaultPath) => {
    if (!isPinnedOriginSender(event) || choosing.has(event.sender.id)) return null;
    choosing.add(event.sender.id);
    try {
      const result = await dialog.showOpenDialog(windowFor(event), {
        properties: ["openDirectory", "createDirectory"],
        defaultPath:
          typeof defaultPath === "string" && path.isAbsolute(defaultPath) ? defaultPath : undefined,
      });
      return result.canceled ? null : (result.filePaths[0] ?? null);
    } finally {
      choosing.delete(event.sender.id);
    }
  });

  // The preload resolves the path; main releases it only to the pinned page.
  ipcMain.handle("omnigent:dropped-file-path", (event, filePath) =>
    isPinnedOriginSender(event) && typeof filePath === "string" ? filePath : "",
  );
}
