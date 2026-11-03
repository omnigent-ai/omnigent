import {
  act,
  cleanup,
  fireEvent,
  render as renderComponent,
  screen,
  waitFor,
} from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/tooltip";
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

const render = (ui: Parameters<typeof renderComponent>[0]) =>
  renderComponent(ui, { wrapper: TooltipProvider });

const fetchBlob = vi.mocked(fetchWorkspaceFileBlob);
const createUrl = vi.fn(() => "blob:recording");
const revokeUrl = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
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
  vi.restoreAllMocks();
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

    fireEvent.error(video);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(container.querySelector("video")).toBeNull();
    expect(revokeUrl).toHaveBeenCalledWith("blob:recording");
    expect(screen.getByRole("button", { name: /Beyond the recording/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Open the app/ })).toHaveAttribute(
      "aria-current",
      "step",
    );
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    await waitFor(() => expect(container.querySelector("video")).not.toBeNull());
    const retryVideo = container.querySelector("video")!;
    fireEvent.loadedMetadata(retryVideo);
    expect(retryVideo.currentTime).toBe(0);
    expect(fetchBlob).toHaveBeenCalledTimes(2);
  });

  it("downloads the original optional WebVTT without fetching video bytes", async () => {
    vi.mocked(useFileContent).mockReturnValue({
      data: { encoding: "utf-8", content: "WEBVTT\n" },
    } as ReturnType<typeof useFileContent>);
    vi.mocked(parseVideoChapters).mockResolvedValue([{ time: 0, end: 5, title: "Open the app" }]);
    vi.mocked(downloadWorkspaceFile).mockResolvedValue();
    render(<VideoPlayer {...props} />);
    const download = await screen.findByRole("button", {
      name: "Download recording: Feature demo",
    });
    fireEvent.keyDown(download, { key: "ArrowDown" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Download annotations (.vtt)" }));
    expect(downloadWorkspaceFile).toHaveBeenCalledWith("sess_1", "demo.webm.chapters.vtt");
    expect(fetchBlob).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Hide recording actions" }));
    expect(download).toBeVisible();
    fireEvent.keyDown(download, { key: "ArrowDown" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Download video" }));
    expect(downloadWorkspaceFile).toHaveBeenLastCalledWith("sess_1", "demo.webm");
  });

  it("keeps navigation controls out of chat and opens the file from its recording title", async () => {
    vi.mocked(useFileContent).mockReturnValue({
      data: { encoding: "utf-8", content: "WEBVTT\n" },
    } as ReturnType<typeof useFileContent>);
    vi.mocked(parseVideoChapters).mockResolvedValue([{ time: 0, end: 5, title: "Open the app" }]);
    const onOpenFile = vi.fn();
    render(<VideoPlayer {...props} compact onOpenFile={onOpenFile} />);
    await screen.findByRole("button", { name: "Download recording: Feature demo" });
    expect(screen.queryByRole("button", { name: /recording actions/ })).toBeNull();
    expect(screen.queryByRole("group")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Feature demo" }));
    expect(onOpenFile).toHaveBeenCalledOnce();
    expect(fetchBlob).not.toHaveBeenCalled();
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

  it("removes cached chapters on a query error and restores them after recovery", async () => {
    const data = { encoding: "utf-8", content: "WEBVTT\n" };
    vi.mocked(useFileContent).mockReturnValue({
      data,
      isError: false,
    } as ReturnType<typeof useFileContent>);
    vi.mocked(parseVideoChapters).mockResolvedValue([{ time: 0, end: 5, title: "Open the app" }]);
    const { container, rerender } = render(<VideoPlayer {...props} />);
    await screen.findByRole("button", { name: /Open the app/ });
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    await waitFor(() => expect(container.querySelector("video")).not.toBeNull());
    const video = container.querySelector("video")!;

    vi.mocked(useFileContent).mockReturnValue({
      data,
      isError: true,
    } as ReturnType<typeof useFileContent>);
    rerender(<VideoPlayer {...props} />);

    expect(screen.queryByRole("group", { name: "Recording actions: Feature demo" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Open the app/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Download recording: Feature demo" })).toBeNull();
    expect(screen.getByRole("button", { name: "Download video: Feature demo" })).toBeVisible();
    expect(container.querySelector("video")).toBe(video);
    expect(video.pause).not.toHaveBeenCalled();
    expect(parseVideoChapters).toHaveBeenCalledOnce();

    vi.mocked(useFileContent).mockReturnValue({
      data,
      isError: false,
    } as ReturnType<typeof useFileContent>);
    rerender(<VideoPlayer {...props} />);
    expect(await screen.findByRole("button", { name: /Open the app/ })).toBeVisible();
    expect(parseVideoChapters).toHaveBeenCalledTimes(2);
  });

  it("discards chapters still being parsed when their query fails", async () => {
    const data = { encoding: "utf-8", content: "WEBVTT\n" };
    vi.mocked(useFileContent).mockReturnValue({ data } as ReturnType<typeof useFileContent>);
    let finish!: (chapters: Awaited<ReturnType<typeof parseVideoChapters>>) => void;
    vi.mocked(parseVideoChapters).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { rerender } = render(<VideoPlayer {...props} />);
    const signal = vi.mocked(parseVideoChapters).mock.calls[0][1];

    vi.mocked(useFileContent).mockReturnValue({
      data,
      isError: true,
    } as ReturnType<typeof useFileContent>);
    rerender(<VideoPlayer {...props} />);
    expect(signal.aborted).toBe(true);
    await act(async () => finish([{ time: 0, end: 5, title: "Stale action" }]));

    expect(screen.queryByRole("group")).toBeNull();
    expect(screen.queryByRole("button", { name: /Stale action/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Download video: Feature demo" })).toBeVisible();
  });

  it("opens the file from its header label", () => {
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
    expect(video.pause).toHaveBeenCalledOnce();
    expect(revokeUrl).toHaveBeenCalledWith("blob:recording");
  });

  it("keeps download available when loading fails and can retry", async () => {
    fetchBlob.mockRejectedValueOnce(new Error("offline"));
    render(<VideoPlayer {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Unable to play this video. Download it to watch locally.",
      ),
    );
    vi.mocked(downloadWorkspaceFile).mockResolvedValueOnce();
    fireEvent.click(screen.getByRole("button", { name: "Download video: Feature demo" }));
    expect(downloadWorkspaceFile).toHaveBeenCalledWith("sess_1", "demo.webm");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    await waitFor(() => expect(fetchBlob).toHaveBeenCalledTimes(2));
  });

  it("loads remote video only on Play and requires another Play after changing source", () => {
    const firstSrc = "https://example.com/demo.mp4?signature=abc";
    const nextSrc = "https://example.com/other.mp4?signature=def";
    const { container, rerender } = render(<VideoPlayer src={firstSrc} title="Demo" />);
    expect(container.querySelector("video")).toBeNull();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Play video: Demo" }));
    const firstVideo = container.querySelector("video")!;
    expect(firstVideo).toHaveAttribute("src", firstSrc);
    expect(firstVideo).toHaveAttribute("controls");
    expect(firstVideo).toHaveAttribute("playsinline");
    expect(firstVideo).not.toHaveAttribute("autoplay");
    expect(firstVideo).not.toHaveAttribute("crossorigin");
    fireEvent.loadedData(firstVideo);
    expect(firstVideo.play).toHaveBeenCalledOnce();

    rerender(<VideoPlayer src={nextSrc} title="Demo" />);
    expect(firstVideo.pause).toHaveBeenCalledOnce();
    expect(container.querySelector("video")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Play video: Demo" }));
    expect(container.querySelector("video")).toHaveAttribute("src", nextSrc);
    expect(container.querySelector("video")).not.toBe(firstVideo);
    expect(fetchBlob).not.toHaveBeenCalled();
    expect(createUrl).not.toHaveBeenCalled();
  });

  it("opens remote video in a new tab without claiming or fetching a download", () => {
    const src = "https://example.com/demo.mp4?signature=abc";
    const popupDocument = document.implementation.createHTMLDocument();
    const popup = { opener: window, document: popupDocument } as Window;
    const open = vi.spyOn(window, "open").mockReturnValue(popup);
    const followedLinks: HTMLAnchorElement[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      followedLinks.push(this);
    });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const { container } = render(<VideoPlayer src={src} title="Demo" />);
    const link = screen.getByRole("link", { name: "Open video in new tab: Demo" });
    expect(link).toHaveAttribute("href", src);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("title", "Open video in new tab");
    expect(link).not.toHaveAttribute("download");
    expect(screen.queryByRole("link", { name: /Download/ })).toBeNull();

    fireEvent.click(link);
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(popup.opener).toBeNull();
    expect(click).toHaveBeenCalledOnce();
    const followedLink = followedLinks[0];
    expect(followedLink.href).toBe(src);
    expect(followedLink.ownerDocument).toBe(popupDocument);
    expect(fetch).not.toHaveBeenCalled();
    expect(fetchBlob).not.toHaveBeenCalled();
    expect(downloadWorkspaceFile).not.toHaveBeenCalled();
    expect(container.querySelector("video")).toBeNull();
  });

  it("offers external opening after remote playback fails and leaves Retry idle until Play", () => {
    const src = "https://example.com/demo.mov";
    const { container, rerender } = render(<VideoPlayer src={src} title="Demo" />);
    fireEvent.click(screen.getByRole("button", { name: "Play video: Demo" }));
    const video = container.querySelector("video")!;
    fireEvent.error(video);
    expect(video.pause).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Unable to play this video here. Try opening it in a new tab.",
    );
    expect(screen.getByRole("status")).not.toHaveTextContent(/Download/);
    expect(screen.getByRole("link", { name: "Open video in new tab: Demo" })).toHaveAttribute(
      "href",
      src,
    );

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.queryByRole("status")).toBeNull();
    expect(container.querySelector("video")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Play video: Demo" }));
    expect(container.querySelector("video")).toHaveAttribute("src", src);
    expect(container.querySelector("video")).not.toBe(video);

    fireEvent.error(container.querySelector("video")!);
    rerender(<VideoPlayer src="https://example.com/next.mp4" title="Next demo" />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(container.querySelector("video")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Play video: Next demo" }));
    expect(container.querySelector("video")).toHaveAttribute("src", "https://example.com/next.mp4");
    expect(fetchBlob).not.toHaveBeenCalled();
  });

  it("cancels unfinished fetches when the source changes or the player closes", async () => {
    let finish!: (blob: Blob) => void;
    fetchBlob.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { rerender, unmount } = render(<VideoPlayer {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    const signal = fetchBlob.mock.calls[0][2]!;
    rerender(<VideoPlayer {...props} path="other.webm" />);
    expect(signal.aborted).toBe(true);
    finish(new Blob(["old video"]));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Play video: Feature demo" })).toBeVisible(),
    );
    expect(createUrl).not.toHaveBeenCalled();
    fetchBlob.mockReturnValueOnce(new Promise(() => {}));
    fireEvent.click(screen.getByRole("button", { name: "Play video: Feature demo" }));
    const closingSignal = fetchBlob.mock.calls[1][2]!;
    unmount();
    expect(closingSignal.aborted).toBe(true);
  });
});
