import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { DEFAULT_CODE_EDITOR_STORAGE_KEY } from "@/lib/editorLinks";
import type { EditorLink, Session } from "@/lib/types";
import { OpenInEditorButton } from "./OpenInEditorButton";

// The desktop host id comes from the Electron bridge; it's an input here, so the
// tests set it directly.
const { desktopHostIdMock } = vi.hoisted(() => ({
  desktopHostIdMock: vi.fn<() => string | null>(() => null),
}));
vi.mock("./RevealInFileManager", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useDesktopHostId: () => desktopHostIdMock(),
}));

const sessionId = "conv_editor";
const remote: EditorLink[] = [
  {
    editor: "cursor",
    label: "Cursor",
    url: "cursor://vscode-remote/ssh-remote+arclet_dev.ssh/home/me/repo",
    local_only: false,
  },
  {
    editor: "vscode",
    label: "VS Code",
    url: "vscode://vscode-remote/ssh-remote+arclet_dev.ssh/home/me/repo",
    local_only: false,
  },
];
const local: EditorLink[] = remote.map((link) => ({
  ...link,
  url: `${link.editor}://file/Users/me/repo`,
  local_only: true,
}));

function renderButton(editorLinks: EditorLink[] | undefined, hostId = "host_1") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["session", sessionId], {
    id: sessionId,
    hostId,
    editorLinks,
  } as Session);
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <OpenInEditorButton sessionId={sessionId} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function launchedUrls(): (string | null)[] {
  return [...document.querySelectorAll("iframe")].map((frame) => frame.getAttribute("src"));
}

afterEach(() => {
  cleanup();
  document.querySelectorAll("iframe").forEach((frame) => frame.remove());
  localStorage.clear();
  desktopHostIdMock.mockReturnValue(null);
});

describe("OpenInEditorButton", () => {
  it("opens the server's first link, unchanged", () => {
    renderButton(remote);
    fireEvent.click(screen.getByRole("button", { name: "Open in Cursor" }));
    expect(launchedUrls()).toEqual([remote[0].url]);
  });

  it("remembers an editor picked from the menu", () => {
    renderButton(remote);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Choose code editor" }), {
      button: 0,
    });
    fireEvent.click(screen.getByRole("menuitemradio", { name: "VS Code" }));
    expect(launchedUrls()).toEqual([remote[1].url]);
    expect(localStorage.getItem(DEFAULT_CODE_EDITOR_STORAGE_KEY)).toBe("vscode");
    expect(screen.getByRole("button", { name: "Open in VS Code" })).toBeInTheDocument();
  });

  it("marks the selected editor on the button and in the menu", () => {
    localStorage.setItem(DEFAULT_CODE_EDITOR_STORAGE_KEY, "vscode");
    renderButton(remote);
    expect(screen.getByRole("button", { name: "Open in VS Code" })).toHaveTextContent("VS Code");

    fireEvent.pointerDown(screen.getByRole("button", { name: "Choose code editor" }), {
      button: 0,
    });
    expect(screen.getByRole("menuitemradio", { name: "VS Code" })).toBeChecked();
    expect(screen.getByRole("menuitemradio", { name: "Cursor" })).not.toBeChecked();
  });

  it("renders nothing when the server offers no links", () => {
    renderButton(undefined);
    expect(screen.queryByTestId("open-in-editor")).toBeNull();
  });

  it("shows local links only on the session's own machine", () => {
    renderButton(local);
    expect(screen.queryByTestId("open-in-editor")).toBeNull();
    cleanup();

    desktopHostIdMock.mockReturnValue("host_1");
    renderButton(local);
    fireEvent.click(screen.getByRole("button", { name: "Open in Cursor" }));
    expect(launchedUrls()).toEqual(["cursor://file/Users/me/repo"]);
  });
});
