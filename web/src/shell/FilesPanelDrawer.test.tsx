import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { FilesPanelDrawer } from "./FilesPanelDrawer";

vi.mock("./FilesPanel", () => ({ FilesPanel: () => <div data-testid="files-content" /> }));
vi.mock("./WorkspaceUploads", () => ({
  WorkspaceUploadProvider: ({ children }: { children: ReactNode }) => children,
  WorkspaceUploadDropRoot: ({ children }: { children: ReactNode }) => (
    <div data-upload-drop-root>{children}</div>
  ),
  UploadsTray: ({ visible }: { visible: boolean }) => (
    <div data-testid="uploads-tray" data-visible={visible} />
  ),
}));
afterEach(cleanup);

it("keeps the shared tray mounted when closing the mobile drawer and wraps only its Files content", () => {
  const tree = (open: boolean) => (
    <MemoryRouter initialEntries={["/c/s"]}>
      <Routes>
        <Route
          path="/c/:conversationId"
          element={
            <FilesPanelDrawer
              open={open}
              onClose={vi.fn()}
              onFileSelect={vi.fn()}
              flatView={false}
              showHidden={false}
              onShowHiddenChange={vi.fn()}
              sort="recent"
              onSortChange={vi.fn()}
            />
          }
        />
      </Routes>
    </MemoryRouter>
  );
  const view = render(tree(true));
  const tray = screen.getByTestId("uploads-tray");
  expect(tray.closest("[data-upload-drop-root]")).toBeNull();
  expect(screen.getByTestId("files-content").closest("[data-upload-drop-root]")).toBeTruthy();
  view.rerender(tree(false));
  expect(tray.isConnected).toBe(true);
  expect(tray.getAttribute("data-visible")).toBe("false");
  expect(screen.queryByTestId("files-content")).toBeNull();
});
