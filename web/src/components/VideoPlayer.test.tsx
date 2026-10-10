import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VideoPlayer } from "./VideoPlayer";
import { parseVideoChapters } from "@/lib/videoChapters";

vi.mock("@/lib/videoChapters", () => ({ parseVideoChapters: vi.fn() }));
import {
  downloadWorkspaceFile,
  fetchWorkspaceFileBlob,
  useFileContent,
} from "@/hooks/useFileContent";

vi.mock("@/hooks/useFileContent", () => ({
  fetchWorkspaceFileBlob: vi.fn(),
  downloadWorkspaceFile: vi.fn(),
  useFileContent: vi.fn(),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const fetchBlob = vi.mocked(fetchWorkspaceFileBlob);
const createUrl = vi.fn(() => "blob:recording");
const revokeUrl = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(parseVideoChapters).mockResolvedValue([]);
  fetchBlob.mockResolvedValue(new Blob(["video"], { type: "video/webm" }));
  vi.mocked(useFileContent).mockReturnValue({ data: undefined } as ReturnType<
    typeof useFileContent
  >);
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
  it("queues a chapter seek before loading and follows native seeking", async () => {
    vi.mocked(useFileContent).mockReturnValue({
      data: {
        encoding: "utf-8",
        content: "WEBVTT\n",
      },
    } as ReturnType<typeof useFileContent>);
    vi.mocked(parseVideoChapters).mockResolvedValue([
      { time: 0, end: 5, title: "Open the app" },
      { time: 5, end: 10, title: "Check the result" },
      { time: 50, end: 60, title: "Beyond the recording" },
    ]);
    const { container } = render(<VideoPlayer {...props} />);
    await screen.findByRole("button", { name: /Check the result/ });
    expect(useFileContent).toHaveBeenCalledWith("sess_1", "demo.webm.chapters.vtt", {
      retry: false,
    });
    fireEvent.click(screen.getByRole("button", { name: /Check the result/ }));
    await waitFor(() => expect(container.querySelector("video")).not.toBeNull());
    const video = container.querySelector("video")!;
    Object.defineProperty(video, "duration", { value: 10 });
    Object.defineProperty(video, "readyState", { value: 2 });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(5);
    expect(screen.getByRole("button", { name: /Beyond the recording/ })).toBeDisabled();
    video.currentTime = 1;
    fireEvent.timeUpdate(video);
    expect(screen.getByRole("button", { name: /Open the app/ })).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: /Check the result/ }));
    expect(video.currentTime).toBe(5);
    expect(screen.getByRole("button", { name: /Check the result/ })).toHaveAttribute(
      "aria-current",
      "step",
    );
  });

  it("leaves malformed or truncated annotations out of the player", () => {
    vi.mocked(useFileContent).mockReturnValue({
      data: {
        encoding: "utf-8",
        truncated: true,
        content: "WEBVTT\n\n00:00.000 --> 00:05.000\nIncomplete\n",
      },
    } as ReturnType<typeof useFileContent>);
    render(<VideoPlayer {...props} />);
    expect(screen.queryByRole("group")).toBeNull();
    expect(screen.getByRole("button", { name: "Play video: Feature demo" })).toBeVisible();
  });

  it("opens the file from its footer label", () => {
    const onOpenFile = vi.fn();
    render(<VideoPlayer {...props} onOpenFile={onOpenFile} />);
    const label = screen.getByRole("button", { name: "Feature demo" });
    expect(label).toHaveAttribute("type", "button");
    fireEvent.click(label);
    expect(onOpenFile).toHaveBeenCalledOnce();
  });

  it("downloads only on Play and exposes native inline controls without autoplay", async () => {
    const { container, unmount } = render(<VideoPlayer {...props} />);
    expect(fetchBlob).not.toHaveBeenCalled();
    expect(screen.queryByRole("group")).toBeNull();
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
