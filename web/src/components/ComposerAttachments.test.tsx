// Pending composer attachments: images render as clickable thumbnails, other
// files as a name+type row, and the blob URL backing an image thumbnail is
// revoked on unmount.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/host", () => ({ getEmbedRoot: () => null }));

import { ImageLightboxProvider } from "./ImageLightbox";
import { ComposerAttachments } from "./ComposerAttachments";

const revoke = vi.fn();

beforeEach(() => {
  // jsdom doesn't implement these; the thumbnail needs both.
  URL.createObjectURL = vi.fn(() => "blob:mock");
  URL.revokeObjectURL = revoke;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  revoke.mockClear();
});

function renderList(files: File[], onRemove = vi.fn()) {
  return {
    onRemove,
    ...render(
      <ImageLightboxProvider>
        <ComposerAttachments files={files} onRemove={onRemove} />
      </ImageLightboxProvider>,
    ),
  };
}

describe("ComposerAttachments", () => {
  it("shows an image as a thumbnail backed by an object URL", () => {
    renderList([new File([new Uint8Array(4)], "shot.png", { type: "image/png" })]);
    const img = screen.getByRole("img", { name: "shot.png" });
    expect(img).toHaveAttribute("src", "blob:mock");
  });

  it("shows a non-image file as a name + type row (no thumbnail)", () => {
    renderList([new File([new Uint8Array(4)], "notes.txt", { type: "text/plain" })]);
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
    expect(screen.getByText("TXT")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("removes the clicked attachment by index", () => {
    const { onRemove } = renderList([
      new File([new Uint8Array(4)], "a.png", { type: "image/png" }),
      new File([new Uint8Array(4)], "b.txt", { type: "text/plain" }),
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Remove b.txt" }));
    expect(onRemove).toHaveBeenCalledWith(1);
  });

  it("revokes the image object URL on unmount so it doesn't leak", () => {
    const { unmount } = renderList([
      new File([new Uint8Array(4)], "shot.png", { type: "image/png" }),
    ]);
    unmount();
    expect(revoke).toHaveBeenCalledWith("blob:mock");
  });
});
