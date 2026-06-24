// Tests for SessionImage — inline preview for a session image file resource.
//
// Two render paths branch on the host config's `fetcher`:
//   - Standalone (no fetcher): a plain same-origin <img src={path}>.
//   - Embedded (fetcher present): bytes are pulled via hostFetch, turned into
//     an object URL, and rendered with explicit loading/loaded/error states.
//
// `@/lib/host` is mocked so each test controls whether a fetcher is installed
// and what hostFetch resolves to; URL.createObjectURL/revokeObjectURL are
// stubbed because jsdom lacks them.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import i18n from "@/i18n";

const t = i18n.getFixedT(null, "common");

const getOmnigentHostConfig = vi.fn();
const hostFetch = vi.fn();

vi.mock("@/lib/host", () => ({
  getOmnigentHostConfig: () => getOmnigentHostConfig(),
  hostFetch: (path: string) => hostFetch(path),
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
    // and emit a direct <img src={path}>, never calling hostFetch.
    render(<SessionImage path="/v1/sessions/a/files/x/content" alt="diagram" className="c" />);
    const img = screen.getByRole("img", { name: "diagram" });
    expect(img).toHaveAttribute("src", "/v1/sessions/a/files/x/content");
    expect(img).toHaveClass("c");
    expect(hostFetch).not.toHaveBeenCalled();
  });
});

describe("SessionImage (embedded, host fetcher present)", () => {
  beforeEach(() => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
  });

  it("shows the loading placeholder before the fetch resolves", () => {
    // WHY: while bytes are in flight the embedded path must render the
    // role="status" placeholder (with spinner), not an <img>.
    hostFetch.mockReturnValue(new Promise(() => {}));
    render(<SessionImage path="/p" alt="pic" />);
    expect(screen.getByRole("status", { name: t("loadingImage") })).toBeInTheDocument();
    expect(screen.getByTestId("spinner")).toBeInTheDocument();
  });

  it("renders the object-URL <img> once the blob loads", async () => {
    // WHY: a successful fetch must create an object URL from the blob and swap
    // the placeholder for an <img src> pointing at it.
    const blob = new Blob(["x"]);
    hostFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    render(<SessionImage path="/p" alt="pic" className="cls" />);
    const img = await screen.findByRole("img", { name: "pic" });
    expect(img).toHaveAttribute("src", "blob:fake-url");
    expect(img).toHaveClass("cls");
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(hostFetch).toHaveBeenCalledWith("/p");
  });

  it("renders the error fallback when the response is not ok", async () => {
    // WHY: a non-ok HTTP response must reject and drop into the error state —
    // a labelled role="img" fallback chip rather than a broken <img>.
    hostFetch.mockResolvedValue({ ok: false, status: 404 });
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
    hostFetch.mockRejectedValue(new Error("boom"));
    render(<SessionImage path="/p" alt="broken" />);
    await waitFor(() => {
      expect(screen.getByRole("img", { name: "broken" })).toHaveTextContent("broken");
    });
  });

  it("renders the error fallback immediately when no path is given", async () => {
    // WHY: an undefined path can't be fetched, so the effect must short-circuit
    // straight to the error state without ever calling hostFetch.
    render(<SessionImage path={undefined} alt="nopath" />);
    await waitFor(() => {
      expect(screen.getByRole("img", { name: "nopath" })).toBeInTheDocument();
    });
    expect(hostFetch).not.toHaveBeenCalled();
  });

  it("revokes the object URL on unmount to avoid leaking blobs", async () => {
    // WHY: the cleanup must release the created object URL; failing to do so
    // leaks blob memory across image swaps.
    const blob = new Blob(["x"]);
    hostFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    const { unmount } = render(<SessionImage path="/p" alt="pic" />);
    await screen.findByRole("img", { name: "pic" });
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:fake-url");
  });
});
