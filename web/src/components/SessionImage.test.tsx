// Tests for SessionImage — inline preview for a session image file resource.
//
// Two render paths branch on the host config's `fetcher`:
//   - Standalone (no fetcher): a plain same-origin <img src={path}>.
//   - Embedded (fetcher present): bytes are pulled via `authenticatedFetch`,
//     turned into an object URL, and rendered with explicit
//     loading/loaded/error states.
//
// The embedded fetch goes through `authenticatedFetch` (not bare `hostFetch`)
// so it inherits the Dicer slice-key header that routes this host-scoped
// file-content read to the replica holding the session's runner tunnel — that
// header injection is covered by identity.test.ts; here we assert delegation.
//
// `@/lib/host` and `@/lib/identity` are mocked so each test controls whether a
// fetcher is installed and what the fetch resolves to; URL.createObjectURL/
// revokeObjectURL are stubbed because jsdom lacks them.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getOmnigentHostConfig = vi.fn();
const authenticatedFetch = vi.fn();

vi.mock("@/lib/host", () => ({
  getOmnigentHostConfig: () => getOmnigentHostConfig(),
}));

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (path: string) => authenticatedFetch(path),
}));

// The Spinner is a brand glyph that renders nothing meaningful in jsdom; a
// marker keeps the loading-state assertion independent of its internals.
vi.mock("@/components/ui/spinner", () => ({
  Spinner: () => <span data-testid="spinner" />,
}));

import { SessionImage } from "./SessionImage";

let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;

beforeEach(() => {
  createObjectURL = vi.fn(() => "blob:fake-url");
  revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("SessionImage (standalone, no host fetcher)", () => {
  beforeEach(() => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: undefined });
  });

  it("renders a plain same-origin <img> pointing at the raw path", () => {
    // WHY: without a host fetcher the component must skip the byte-fetch path
    // and emit a direct <img src={path}>, never calling authenticatedFetch.
    render(<SessionImage path="/v1/sessions/a/files/x/content" alt="diagram" className="c" />);
    const img = screen.getByRole("img", { name: "diagram" });
    expect(img).toHaveAttribute("src", "/v1/sessions/a/files/x/content");
    expect(img).toHaveClass("c");
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("SessionImage (embedded, host fetcher present)", () => {
  beforeEach(() => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
  });

  it("shows the loading placeholder before the fetch resolves", () => {
    // WHY: while bytes are in flight the embedded path must render the
    // role="status" placeholder (with spinner), not an <img>.
    authenticatedFetch.mockReturnValue(new Promise(() => {}));
    render(<SessionImage path="/p" alt="pic" />);
    expect(screen.getByRole("status", { name: "Loading image" })).toBeInTheDocument();
    expect(screen.getByTestId("spinner")).toBeInTheDocument();
  });

  it("fetches the blob via authenticatedFetch so the slice-key header rides", async () => {
    // WHY: the embedded read must go through authenticatedFetch (not bare
    // hostFetch) — that seam stamps the Dicer slice key for the session path,
    // routing this host-scoped file-content read to the runner-tunnel replica.
    // On success it creates an object URL from the blob and renders the <img>.
    const blob = new Blob(["x"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    render(
      <SessionImage
        path="/v1/sessions/conv_abc/resources/files/file_1/content"
        alt="pic"
        className="cls"
      />,
    );
    const img = await screen.findByRole("img", { name: "pic" });
    expect(img).toHaveAttribute("src", "blob:fake-url");
    expect(img).toHaveClass("cls");
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv_abc/resources/files/file_1/content",
    );
  });

  it("renders the error fallback when the response is not ok", async () => {
    // WHY: a non-ok HTTP response must reject and drop into the error state —
    // a labelled role="img" fallback chip rather than a broken <img>.
    authenticatedFetch.mockResolvedValue({ ok: false, status: 404 });
    render(<SessionImage path="/missing" alt="gone" />);
    await waitFor(() => {
      const fallback = screen.getByRole("img", { name: "gone" });
      expect(fallback).not.toHaveAttribute("src");
      expect(fallback).toHaveTextContent("gone");
    });
  });

  it("renders the error fallback when the fetch rejects", async () => {
    // WHY: a network rejection (vs. an HTTP error) must land in the same error
    // fallback rather than surfacing an unhandled rejection.
    authenticatedFetch.mockRejectedValue(new Error("boom"));
    render(<SessionImage path="/p" alt="broken" />);
    await waitFor(() => {
      expect(screen.getByRole("img", { name: "broken" })).toHaveTextContent("broken");
    });
  });

  it("renders the error fallback immediately when no path is given", async () => {
    // WHY: an undefined path can't be fetched, so the effect must short-circuit
    // straight to the error state without ever calling authenticatedFetch.
    render(<SessionImage path={undefined} alt="nopath" />);
    await waitFor(() => {
      expect(screen.getByRole("img", { name: "nopath" })).toBeInTheDocument();
    });
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("revokes the object URL on unmount to avoid leaking blobs", async () => {
    // WHY: the cleanup must release the created object URL; failing to do so
    // leaks blob memory across image swaps.
    const blob = new Blob(["x"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    const { unmount } = render(<SessionImage path="/p" alt="pic" />);
    await screen.findByRole("img", { name: "pic" });
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:fake-url");
  });
});
