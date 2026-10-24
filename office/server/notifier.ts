// Desktop notifications for requests that need the PO while the browser is
// closed. Notifications link nowhere actionable: decisions happen in the room.

import { execFile } from "node:child_process";

export interface Notifier {
  notify(title: string, body: string): void;
}

export const silentNotifier: Notifier = { notify: () => undefined };

/** Uses the platform's notification command; failures are ignored. */
export function desktopNotifier(platform: NodeJS.Platform = process.platform): Notifier {
  return {
    notify(title, body) {
      const clippedTitle = title.slice(0, 120);
      const clippedBody = body.slice(0, 240);
      if (platform === "darwin") {
        // Arguments go through argv, never string interpolation into the script.
        const script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run";
        execFile("osascript", ["-e", script, clippedTitle, clippedBody], () => undefined);
      } else if (platform === "linux") {
        execFile("notify-send", ["--app-name=Omnigent Office", clippedTitle, clippedBody], () => undefined);
      }
    },
  };
}
