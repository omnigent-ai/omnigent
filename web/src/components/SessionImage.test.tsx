// Tests for SessionImage — inline preview for a session image file resource.
//
// Two render paths branch on the host config's `fetcher`:
//   - Standalone (no fetcher): a plain same-origin <img src={path}>.
//   - Embedded (fetcher present): bytes are pulled via authenticatedFetch, turned into
//     an object URL, and rendered with explicit loading/loaded/error states.
//
// `@/lib/host` is mocked so each test controls whether a fetcher is installed
// and what authenticatedFetch resolves to; URL.createObjectURL/revokeObjectURL are
// stubbed because jsdom lacks them.

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

import type { SessionImage as SessionImageComponent } from "./SessionImage";

// The component caches object URLs in module scope so they survive remounts,
// so each test needs a fresh module instance — otherwise one test's cached
// image satisfies the next test's render and its authenticatedFetch mock never runs.
let SessionImage: typeof SessionImageComponent;

let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  // Import before stubbing: the stub replaces the whole URL global, and the
  // module graph needs the real constructor while it loads.
  vi.resetModules();
  ({ SessionImage } = await import("./SessionImage"));
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

  it("reserves the preview box height before the image loads", () => {
    // WHY: the chat scroller runs with overflow-anchor:none, so an image that
    // sized itself only on decode would push the transcript down under the
    // reader. The enclosing box must carry a fixed height from first paint.
    render(<SessionImage path="/v1/sessions/a/files/x/content" alt="diagram" />);
    const box = screen.getByRole("img", { name: "diagram" }).closest("div");
    expect(box).toHaveClass("h-64");
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

  it("renders the object-URL <img> once the blob loads", async () => {
    // WHY: a successful fetch must create an object URL from the blob and swap
    // the placeholder for an <img src> pointing at it.
    const blob = new Blob(["x"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    render(<SessionImage path="/p" alt="pic" className="cls" />);
    const img = await screen.findByRole("img", { name: "pic" });
    expect(img).toHaveAttribute("src", "blob:fake-url");
    expect(img).toHaveClass("cls");
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(authenticatedFetch).toHaveBeenCalledWith("/p");
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

  it("reuses the cached object URL across remounts instead of refetching", async () => {
    // WHY: a file id addresses immutable bytes, so re-opening a session must
    // repaint from the cached blob — refetching megabytes per remount is what
    // made transcripts slow. The URL therefore has to survive unmount.
    const blob = new Blob(["x"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    const first = render(<SessionImage path="/cached" alt="pic" />);
    await screen.findByRole("img", { name: "pic" });
    first.unmount();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    render(<SessionImage path="/cached" alt="pic" />);
    // Paints straight from cache — no placeholder frame, no second download.
    expect(screen.getByRole("img", { name: "pic" })).toHaveAttribute("src", "blob:fake-url");
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it("shares one download between concurrent mounts of the same path", async () => {
    // WHY: the same attachment can mount twice in a frame (a switch back while
    // the first fetch is still open); both must join the in-flight request.
    const blob = new Blob(["x"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    render(
      <>
        <SessionImage path="/shared" alt="one" />
        <SessionImage path="/shared" alt="two" />
      </>,
    );
    await screen.findByRole("img", { name: "one" });
    await screen.findByRole("img", { name: "two" });
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("evicts and revokes the oldest image once the cache bound is passed", async () => {
    // WHY: cached URLs deliberately outlive their components, so without a
    // bound a long session would leak every image it ever rendered.
    const blob = new Blob(["x"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    const created: string[] = [];
    createObjectURL.mockImplementation(() => {
      const url = `blob:evict-${created.length}`;
      created.push(url);
      return url;
    });
    // One past the 32-entry bound, so the first of these must fall out. They
    // cache in mount order, since each fetch resolves in the order it started.
    const paths = Array.from({ length: 33 }, (_, i) => `/evict-${i}`);
    render(
      <>
        {paths.map((path, i) => (
          <SessionImage key={path} path={path} alt={`pic-${i}`} />
        ))}
      </>,
    );
    await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(33));
    expect(revokeObjectURL).toHaveBeenCalledWith(created[0]);
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
  });
});
