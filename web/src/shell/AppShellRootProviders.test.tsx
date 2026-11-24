import { cleanup, render, screen } from "@testing-library/react";
import type { ContextType } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppShellRootProviders } from "./AppShellRootProviders";
import { commandSessionFromSnapshot, useCommandContext } from "./commandContext";
import { type FileViewerContext, useFileViewer } from "./FileViewerContext";

afterEach(cleanup);

function Reader() {
  const context = useCommandContext();
  const openFile = useFileViewer();
  return (
    <p>
      {[context.session?.id, context.view, context.workspaceTab, context.selection.filePath].join(
        " ",
      )}
      {openFile ? " can open files" : ""}
    </p>
  );
}

describe("AppShellRootProviders", () => {
  it("gives ⌘K commands the shell's session, view, tab and file, and the file viewer", () => {
    const fileViewer = { openFile: vi.fn() } as unknown as NonNullable<
      ContextType<typeof FileViewerContext>
    >;
    render(
      <AppShellRootProviders
        command={{
          session: commandSessionFromSnapshot("s1", null),
          view: "session",
          workspaceTab: "files",
          filePath: "src/app.ts",
        }}
        fileViewer={fileViewer}
      >
        <Reader />
      </AppShellRootProviders>,
    );
    expect(screen.getByText("s1 session files src/app.ts can open files")).toBeTruthy();
  });
});
