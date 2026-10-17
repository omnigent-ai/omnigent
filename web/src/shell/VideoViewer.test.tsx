import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { VideoViewer } from "./VideoViewer";
import {
  downloadWorkspaceFile,
  fetchWorkspaceFileBlob,
  usesDirectFileDownload,
} from "@/hooks/useFileContent";

vi.mock("@/hooks/useFileContent", () => ({
  workspaceFileDownloadUrl: (session: string, path: string) =>
    `/raw/${session}/${path}?download=true`,
  usesDirectFileDownload: vi.fn(() => true),
  fetchWorkspaceFileBlob: vi.fn(),
  downloadWorkspaceFile: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

beforeEach(() => {
  vi.mocked(usesDirectFileDownload).mockReturnValue(true);
  vi.mocked(fetchWorkspaceFileBlob).mockReset();
  vi.mocked(downloadWorkspaceFile).mockReset();
  vi.mocked(downloadWorkspaceFile).mockResolvedValue(undefined);
  vi.mocked(toast.error).mockClear();
  window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:video"), revokeObjectURL: vi.fn() });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OMNIGENT_BASE_PATH__;
});

describe("VideoViewer", () => {
  it("uses a direct base-prefixed stream with native controls", () => {
    const { container } = render(<VideoViewer conversationId="sess" path="clip.mp4" />);
    const video = container.querySelector("video");
    expect(video).toHaveAttribute("src", "/proxy/6767/raw/sess/clip.mp4?download=true");
    expect(video).toHaveAttribute("controls");
    expect(video).toHaveAttribute("playsinline");
    expect(video).toHaveAttribute("preload", "metadata");
    expect(fetchWorkspaceFileBlob).not.toHaveBeenCalled();
  });
  it("shows loading, uses an authenticated blob, and revokes on unmount", async () => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(false);
    let finish: (blob: Blob) => void = () => {};
    vi.mocked(fetchWorkspaceFileBlob).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { container, unmount } = render(<VideoViewer conversationId="sess" path="clip.webm" />);
    expect(screen.getByText("Loading video…")).toBeInTheDocument();
    await act(async () => finish(new Blob(["video"])));
    expect(container.querySelector("video")).toHaveAttribute("src", "blob:video");
    expect(fetchWorkspaceFileBlob).toHaveBeenCalledWith(
      "sess",
      "clip.webm",
      expect.any(AbortSignal),
    );
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:video");
  });
  it("revokes the previous blob when the path changes", async () => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(false);
    vi.mocked(fetchWorkspaceFileBlob).mockResolvedValue(new Blob(["video"]));
    const { rerender } = render(<VideoViewer conversationId="sess" path="first.webm" />);
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(1));
    rerender(<VideoViewer conversationId="sess" path="next.webm" />);
    await waitFor(() =>
      expect(fetchWorkspaceFileBlob).toHaveBeenCalledWith(
        "sess",
        "next.webm",
        expect.any(AbortSignal),
      ),
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:video");
  });
  it("does not create an object URL after an unmounted fetch completes", async () => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(false);
    let finish: (blob: Blob) => void = () => {};
    vi.mocked(fetchWorkspaceFileBlob).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { unmount } = render(<VideoViewer conversationId="sess" path="clip.webm" />);
    unmount();
    await act(async () => finish(new Blob(["video"])));
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
  it.each(["unmount", "path change"])("aborts an in-flight blob fetch on %s", async (change) => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(false);
    const signals: (AbortSignal | undefined)[] = [];
    vi.mocked(fetchWorkspaceFileBlob).mockImplementation(
      (_conversationId, _path, signal) =>
        new Promise((_resolve, reject) => {
          signals.push(signal);
          signal?.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            {
              once: true,
            },
          );
        }),
    );
    const { rerender, unmount } = render(<VideoViewer conversationId="sess" path="first.webm" />);
    const originalSignal = signals[0];
    expect(originalSignal).toBeInstanceOf(AbortSignal);
    expect(originalSignal?.aborted).toBe(false);

    await act(async () => {
      if (change === "unmount") unmount();
      else rerender(<VideoViewer conversationId="sess" path="next.webm" />);
    });

    expect(originalSignal?.aborted).toBe(true);
    expect(screen.queryByText("This video can't be played here.")).not.toBeInTheDocument();
    if (change === "path change") {
      expect(signals[1]?.aborted).toBe(false);
      expect(screen.getByText("Loading video…")).toBeInTheDocument();
    }
  });
  it("shows the error state for an AbortError not caused by cleanup", async () => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(false);
    vi.mocked(fetchWorkspaceFileBlob).mockRejectedValue(new DOMException("Aborted", "AbortError"));

    await act(async () => render(<VideoViewer conversationId="sess" path="clip.webm" />));

    expect(vi.mocked(fetchWorkspaceFileBlob).mock.calls[0]?.[2]?.aborted).toBe(false);
    expect(screen.getByText("This video can't be played here.")).toBeInTheDocument();
    expect(screen.queryByText("Loading video…")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(downloadWorkspaceFile).toHaveBeenCalledWith("sess", "clip.webm");
  });
  it("shows the toolbar failure toast when fallback Download rejects", async () => {
    vi.mocked(downloadWorkspaceFile).mockRejectedValue(new Error("offline"));
    const { container } = render(<VideoViewer conversationId="sess" path="bad.mov" />);
    fireEvent.error(container.querySelector("video")!);

    fireEvent.click(screen.getByRole("button", { name: "Download" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Download failed"));
  });
  it.each(["media", "fetch"])("offers a working Download after a %s error", async (failure) => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(failure === "media");
    vi.mocked(fetchWorkspaceFileBlob).mockRejectedValue(new Error("offline"));
    const { container } = render(<VideoViewer conversationId="sess" path="/tmp/bad.mov" />);
    if (failure === "media") fireEvent.error(container.querySelector("video")!);
    expect(await screen.findByText("This video can't be played here.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(downloadWorkspaceFile).toHaveBeenCalledWith("sess", "/tmp/bad.mov");
  });
});
