import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

/** Reads a path's Spotlight content-type tree, or null without metadata. */
export type ContentTypesReader = (filePath: string) => Promise<string | null>;

/** The part of Electron's `shell` used to reveal files. */
export interface RevealShell {
  showItemInFolder: (fullPath: string) => void;
  openPath: (path: string) => Promise<string>;
}

export interface FileRevealDeps<Event> {
  ipcMain: {
    handle: (channel: string, listener: (event: Event, ...args: unknown[]) => unknown) => void;
  };
  shell: RevealShell;
  isPinnedOriginSender: (event: Event) => boolean;
  localHostId: () => string;
  platform?: NodeJS.Platform;
  contentTypes?: ContentTypesReader;
}

/** Spotlight's type tree for a path, or null when it has no metadata (unindexed volume). */
function macContentTypes(filePath: string): Promise<string | null> {
  return new Promise((resolve) => {
    execFile(
      "mdls",
      ["-raw", "-name", "kMDItemContentTypeTree", filePath],
      { timeout: 2000 },
      (error, stdout) => resolve(error || stdout.trim() === "(null)" ? null : stdout),
    );
  });
}

/** Whether a macOS directory is a package (Foo.app, Deck.key) that openPath would launch. */
async function isMacPackage(dir: string, contentTypes: ContentTypesReader): Promise<boolean> {
  if (fs.existsSync(path.join(dir, "Contents", "Info.plist"))) return true;
  const types = await contentTypes(dir);
  // Without metadata, a dotted name may be a package; selecting it is the safe miss.
  return types === null ? path.extname(dir) !== "" : types.includes("com.apple.package");
}

/**
 * Reveal this machine's files in the OS file manager, never executing them: a
 * file, link, or macOS package is selected in its folder, and a folder is opened.
 */
export function registerFileReveal<Event>({
  ipcMain,
  shell,
  isPinnedOriginSender,
  localHostId,
  platform = process.platform,
  contentTypes = macContentTypes,
}: FileRevealDeps<Event>): void {
  ipcMain.handle("omnigent:reveal-file", async (event, hostId, rawPath) => {
    if (!isPinnedOriginSender(event)) return false;
    if (typeof hostId !== "string" || !hostId || hostId !== localHostId()) return false;
    if (typeof rawPath !== "string" || rawPath.includes("\0") || !path.isAbsolute(rawPath)) {
      return false;
    }
    // The SPA joins paths with "/"; normalize to this platform's separators.
    const filePath = path.normalize(rawPath);
    try {
      // lstat: a link is selected, never followed, so it can't open what it points at.
      const stat = fs.lstatSync(filePath);
      if (
        !stat.isDirectory() ||
        (platform === "darwin" && (await isMacPackage(filePath, contentTypes)))
      ) {
        shell.showItemInFolder(filePath);
        return true;
      }
      // Resolves "" on success, otherwise the platform's error message.
      return (await shell.openPath(filePath)) === "";
    } catch {
      return false;
    }
  });
}
