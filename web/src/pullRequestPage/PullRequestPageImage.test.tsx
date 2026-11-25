import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as DesktopReadPathModule from "@/lib/desktopReadPath";

import type { ReadSource } from "./useReadSource";

const state = vi.hoisted(() => ({
  scope: "scope-1" as string | null,
  macLoads: true,
  source: { offered: true, choice: "automatic", choose: vi.fn() } as Partial<ReadSource>,
  load: vi.fn(),
  release: vi.fn(),
  retain: vi.fn(),
  forget: vi.fn(),
}));

vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopReadPathModule>()),
  macReadCandidate: () => ({
    provider: "github",
    host: "github.com",
    repository: "octo/private",
    number: 370,
    url: PR_URL,
  }),
  useDesktopReadPathVersion: () => 0,
}));
vi.mock("./useReadSource", () => ({ useReadSource: () => state.source }));
vi.mock("./pullRequestImages", () => ({
  pullRequestImageScope: () => state.scope,
  macLoadsPullRequestImages: () => state.macLoads,
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
    onPage ? <PullRequestImageScope prUrl={PR_URL}>{image}</PullRequestImageScope> : image,
  );
}

beforeEach(() => {
  state.scope = "scope-1";
  state.macLoads = true;
  state.source = { offered: true, choice: "automatic", choose: vi.fn() };
  state.load.mockReset();
  state.release.mockReset();
  state.retain.mockReset();
  state.forget.mockReset();
});

describe("PullRequestPageImage on this Mac's route", () => {
  it("shows the copy the shell loaded, never the asset URL", async () => {
    state.load.mockResolvedValue({ kind: "loaded", url: "blob:image-1" });
    renderImage();
    expect(screen.getByText("Loading image…")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Screenshot" })).toHaveAttribute(
      "src",
      "blob:image-1",
    );
    expect(state.load).toHaveBeenCalledWith(expect.objectContaining({ url: PR_URL }), ASSET);
  });

  it("drops a loaded copy that won't show, and Try again reads it afresh", async () => {
    state.load.mockResolvedValueOnce({ kind: "loaded", url: "blob:broken" });
    renderImage();
    fireEvent.error(await screen.findByRole("img", { name: "Screenshot" }));
    expect(state.forget).toHaveBeenCalledWith(expect.objectContaining({ url: PR_URL }), ASSET);
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
    expect(screen.queryByRole("button", { name: "Read on this Mac" })).toBeNull();
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
      <PullRequestImageScope prUrl={`${PR_URL}/files`}>
        <Image src={ASSET} alt="Screenshot" />
      </PullRequestImageScope>,
    );
    expect(state.retain).toHaveBeenCalledWith(PR_URL);
  });
});

describe("PullRequestPageImage off this Mac's route", () => {
  beforeEach(() => {
    state.scope = null;
  });

  it("tries the image directly, then offers to read the PR on this Mac", async () => {
    renderImage();
    const image = screen.getByRole("img", { name: "Screenshot" });
    expect(image).toHaveAttribute("src", ASSET);
    expect(state.load).not.toHaveBeenCalled();
    fireEvent.error(image);
    expect(
      screen.getByText(
        "Images in private repositories load when this pull request is read on this Mac.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Read on this Mac" }));
    expect(state.source.choose).toHaveBeenCalledWith("mac");
  });

  for (const [label, setup] of [
    ["an older shell without the image read", () => (state.macLoads = false)],
    ["the browser build", () => (state.source = { offered: false, choice: "automatic" })],
    ["a Mac choice this Mac can't read", () => (state.source = { offered: true, choice: "mac" })],
  ] as const) {
    it(`offers only Open on GitHub in ${label}`, () => {
      setup();
      renderImage();
      fireEvent.error(screen.getByRole("img", { name: "Screenshot" }));
      expect(screen.getByText("This image can't be shown here.")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Read on this Mac" })).toBeNull();
      expect(screen.getByRole("link", { name: "Open on GitHub" })).toHaveAttribute("href", ASSET);
    });
  }
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

it("waits for the shell before showing anything else", async () => {
  state.load.mockResolvedValue({ kind: "loaded", url: "blob:image-1" });
  renderImage();
  await waitFor(() => expect(screen.queryByText("Loading image…")).toBeNull());
});
