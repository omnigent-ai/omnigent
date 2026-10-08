import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VideoPlayer } from "./VideoPlayer";
import { downloadWorkspaceFile, fetchWorkspaceFileBlob } from "@/hooks/useFileContent";

vi.mock("@/hooks/useFileContent", () => ({
  fetchWorkspaceFileBlob: vi.fn(),
  downloadWorkspaceFile: vi.fn(),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const fetchBlob = vi.mocked(fetchWorkspaceFileBlob);
const createUrl = vi.fn(() => "blob:recording");
const revokeUrl = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  fetchBlob.mockResolvedValue(new Blob(["video"], { type: "video/webm" }));
  vi.stubGlobal(
    "URL",
    class extends URL {
      static override createObjectURL = createUrl;
      static override revokeObjectURL = revokeUrl;
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const props = { conversationId: "sess_1", path: "demo.webm", title: "Feature demo" };

describe("VideoPlayer", () => {
  it("downloads only on Play and exposes native inline controls without autoplay", async () => {
    const { container, unmount } = render(<VideoPlayer {...props} />);
    expect(fetchBlob).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading video");
    await waitFor(() =>
      expect(container.querySelector("video")).toHaveAttribute("src", "blob:recording"),
    );
    expect(fetchBlob).toHaveBeenCalledWith("sess_1", "demo.webm", expect.any(AbortSignal));
    const video = container.querySelector("video")!;
    expect(video).toHaveAttribute("controls");
    expect(video).toHaveAttribute("playsinline");
    expect(video).not.toHaveAttribute("autoplay");
    unmount();
    expect(revokeUrl).toHaveBeenCalledWith("blob:recording");
  });

  it("keeps download available when loading fails and can retry", async () => {
    fetchBlob.mockRejectedValueOnce(new Error("offline"));
    render(<VideoPlayer {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Unable to play/));
    vi.mocked(downloadWorkspaceFile).mockResolvedValueOnce();
    fireEvent.click(screen.getByRole("button", { name: "Download video: Feature demo" }));
    expect(downloadWorkspaceFile).toHaveBeenCalledWith("sess_1", "demo.webm");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    await waitFor(() => expect(fetchBlob).toHaveBeenCalledTimes(2));
  });

  it("shows a fallback when the browser cannot decode the codec", () => {
    const { container } = render(<VideoPlayer src="https://example.com/demo.mov" title="Demo" />);
    fireEvent.error(container.querySelector("video")!);
    expect(screen.getByRole("status")).toHaveTextContent(/Download it/);
    expect(screen.getByRole("link", { name: "Download video: Demo" })).toHaveAttribute(
      "href",
      "https://example.com/demo.mov",
    );
  });

  it("cancels and discards an unfinished fetch when the source changes", async () => {
    let finish!: (blob: Blob) => void;
    fetchBlob.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { rerender } = render(<VideoPlayer {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    const signal = fetchBlob.mock.calls[0][2]!;
    rerender(<VideoPlayer {...props} path="other.webm" />);
    expect(signal.aborted).toBe(true);
    finish(new Blob(["old video"]));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Play video: Feature demo" })).toBeVisible(),
    );
    expect(createUrl).not.toHaveBeenCalled();
  });
});
