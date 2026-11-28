import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  scope: "scope-1" as string | null,
  served: true,
  load: vi.fn(),
  release: vi.fn(),
  retain: vi.fn(),
  forget: vi.fn(),
}));

// Whether the session's host reads attachments; each test says.
vi.mock("./pullRequestPageRoute", () => ({
  usePullRequestMethodServed: (session: string, method: string) =>
    state.served && session === "conv_1" && method === "readPullRequestImage",
}));
vi.mock("./pullRequestImages", () => ({
  pullRequestImageScope: (target: unknown) => (target ? state.scope : null),
  loadPullRequestImage: (...args: unknown[]) => state.load(...args),
  forgetPullRequestImage: (...args: unknown[]) => state.forget(...args),
  retainPullRequestImages: (url: string) => {
    state.retain(url);
    return () => state.release(url);
  },
}));

import { PULL_REQUEST_MARKDOWN_COMPONENTS } from "@/newPullRequestPage/shell/PullRequestDetails";

import { PullRequestImageScope } from "./PullRequestPageImage";

const PR_URL = "https://github.com/octo/private/pull/370";
const ASSET = "https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab";
const Image = PULL_REQUEST_MARKDOWN_COMPONENTS.img as React.ComponentType<
  React.ComponentProps<"img">
>;

function renderImage({ onPage = true, src = ASSET } = {}) {
  const image = <Image src={src} alt="Screenshot" />;
  return render(
    onPage ? (
      <PullRequestImageScope conversationId="conv_1" prUrl={PR_URL}>
        {image}
      </PullRequestImageScope>
    ) : (
      image
    ),
  );
}

beforeEach(() => {
  state.scope = "scope-1";
  state.served = true;
  state.load.mockReset();
  state.release.mockReset();
  state.retain.mockReset();
  state.forget.mockReset();
});

describe("PullRequestPageImage where the session's host reads attachments", () => {
  it("shows the copy the host loaded, never the asset URL", async () => {
    state.load.mockResolvedValue({ kind: "loaded", url: "blob:image-1" });
    renderImage();
    expect(screen.getByText("Loading image…")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Screenshot" })).toHaveAttribute(
      "src",
      "blob:image-1",
    );
    expect(state.load).toHaveBeenCalledWith({ conversationId: "conv_1", prUrl: PR_URL }, ASSET);
  });

  it("drops a loaded copy that won't show, and Try again reads it afresh", async () => {
    state.load.mockResolvedValueOnce({ kind: "loaded", url: "blob:broken" });
    renderImage();
    fireEvent.error(await screen.findByRole("img", { name: "Screenshot" }));
    expect(state.forget).toHaveBeenCalledWith({ conversationId: "conv_1", prUrl: PR_URL }, ASSET);
    state.load.mockResolvedValueOnce({ kind: "loaded", url: "blob:fresh" });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("img", { name: "Screenshot" })).toHaveAttribute(
      "src",
      "blob:fresh",
    );
    expect(state.load).toHaveBeenCalledTimes(2);
  });

  it("loads an attachment the PR doesn't sign directly, as a public one", async () => {
    state.load.mockResolvedValue({ kind: "failed", reason: "not_found" });
    renderImage();
    expect(await screen.findByRole("img", { name: "Screenshot" })).toHaveAttribute("src", ASSET);
  });

  it("offers Try again and Open on GitHub, not a switch, when loading fails", async () => {
    state.load.mockResolvedValueOnce({ kind: "failed", reason: "expired" });
    renderImage();
    expect(
      await screen.findByText("GitHub's link to this image expired before it loaded."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", ASSET);
    state.load.mockResolvedValueOnce({ kind: "loaded", url: "blob:image-2" });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("img", { name: "Screenshot" })).toHaveAttribute(
      "src",
      "blob:image-2",
    );
    expect(state.load).toHaveBeenCalledTimes(2);
  });

  it("holds the PR before its images load, and releases it when the page leaves", () => {
    state.load.mockImplementation(() => {
      expect(state.retain).toHaveBeenCalledWith(PR_URL);
      return new Promise(() => {});
    });
    const { unmount } = renderImage();
    expect(state.load).toHaveBeenCalledTimes(1);
    unmount();
    expect(state.release).toHaveBeenCalledWith(PR_URL);
  });

  it("releases the PR when the new page turns off, keeping the image shown", () => {
    state.load.mockReturnValue(new Promise(() => {}));
    const { rerender } = renderImage();
    rerender(<Image src={ASSET} alt="Screenshot" />);
    expect(state.release).toHaveBeenCalledWith(PR_URL);
    expect(screen.getByRole("img", { name: "Screenshot" })).toHaveAttribute("src", ASSET);
  });

  it("holds a subpage's PR by the PR's own link", () => {
    state.load.mockReturnValue(new Promise(() => {}));
    render(
      <PullRequestImageScope conversationId="conv_1" prUrl={`${PR_URL}/files`}>
        <Image src={ASSET} alt="Screenshot" />
      </PullRequestImageScope>,
    );
    expect(state.retain).toHaveBeenCalledWith(PR_URL);
  });
});

describe("PullRequestPageImage where the host doesn't read attachments", () => {
  beforeEach(() => {
    state.served = false;
  });

  it("tries the image directly, then offers only Open on GitHub", () => {
    renderImage();
    const image = screen.getByRole("img", { name: "Screenshot" });
    expect(image).toHaveAttribute("src", ASSET);
    expect(state.load).not.toHaveBeenCalled();
    fireEvent.error(image);
    expect(screen.getByText("This image can't be shown here.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", ASSET);
  });
});

describe("PullRequestImage outside the new page", () => {
  it("keeps the old behavior", () => {
    renderImage({ onPage: false });
    fireEvent.error(screen.getByRole("img", { name: "Screenshot" }));
    expect(screen.getByText("Image not available")).toBeInTheDocument();
    expect(state.load).not.toHaveBeenCalled();
    expect(state.release).not.toHaveBeenCalled();
  });
});

it("waits for the host before showing anything else", async () => {
  state.load.mockResolvedValue({ kind: "loaded", url: "blob:image-1" });
  renderImage();
  await waitFor(() => expect(screen.queryByText("Loading image…")).toBeNull());
});
