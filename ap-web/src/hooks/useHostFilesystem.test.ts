// Unit tests for the URL builder used by the host filesystem hook.
// Pins the path encoding so a regression doesn't silently produce
// URLs that the FastAPI route rejects (404) or that double-encode
// segments (resulting in literal "%2F" reaching the host).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HostFilesystemEntry } from "./useHostFilesystem";
import { buildHostFilesystemUrl, useHostFilesystem } from "./useHostFilesystem";

describe("buildHostFilesystemUrl", () => {
  it("returns the no-path endpoint when absolutePath is empty", () => {
    // Empty absolute path means "browse the host's home"; the
    // server's /v1/hosts/{id}/filesystem route forwards ~ to
    // host.list_dir for that case.
    expect(buildHostFilesystemUrl("host_abc", "")).toBe("/v1/hosts/host_abc/filesystem");
  });

  it("strips the single leading slash (FastAPI re-adds it)", () => {
    // The route is /filesystem/{path:path}; FastAPI strips the
    // first "/" before passing the path through to the handler,
    // and the handler re-adds it. So we send "Users/corey/foo"
    // not "/Users/corey/foo" — sending the latter would result
    // in "//Users/corey/foo" reaching the host.
    expect(buildHostFilesystemUrl("host_abc", "/Users/corey/foo")).toBe(
      "/v1/hosts/host_abc/filesystem/Users/corey/foo",
    );
  });

  it("encodes special characters per segment", () => {
    // Spaces and other URL-meaningful chars must round-trip
    // through encodeURIComponent. Without encoding, a name like
    // "my project" would produce a malformed URL.
    expect(buildHostFilesystemUrl("host_abc", "/Users/c o/foo bar")).toBe(
      "/v1/hosts/host_abc/filesystem/Users/c%20o/foo%20bar",
    );
  });

  it("preserves slashes between segments", () => {
    // encodeURIComponent encodes "/" to %2F; we encode per
    // segment then rejoin with "/" so the directory hierarchy
    // survives. Pinning this catches a regression where someone
    // calls encodeURIComponent on the whole string at once.
    const url = buildHostFilesystemUrl("host_abc", "/a/b/c");
    expect(url).toBe("/v1/hosts/host_abc/filesystem/a/b/c");
    expect(url).not.toContain("%2F");
  });

  it("encodes the host id as well", () => {
    // Host ids are server-generated and don't contain weird
    // chars in practice, but encoding them is the right thing
    // and protects against future ID-format changes.
    expect(buildHostFilesystemUrl("host with space", "")).toBe(
      "/v1/hosts/host%20with%20space/filesystem",
    );
  });

  it("preserves a single trailing slash for the root", () => {
    // Browsing exactly "/" must hit /filesystem/ (with trailing
    // slash) to match the {path:path} route. Without the trailing
    // slash we'd hit the no-path route which forwards ~ instead.
    expect(buildHostFilesystemUrl("host_abc", "/")).toBe("/v1/hosts/host_abc/filesystem/");
  });
});

// ---------------------------------------------------------------------------
// useHostFilesystem — pagination, truncation, error, and lazy-enable behavior.
// Exercised through the hook since fetchHostFilesystem is module-private.
// authenticatedFetch ultimately calls the global fetch, so we stub that.
// ---------------------------------------------------------------------------

function entry(name: string): HostFilesystemEntry {
  return { name, path: `/d/${name}`, type: "directory", bytes: null, modified_at: 0 };
}

function pageResponse(data: HostFilesystemEntry[], hasMore: boolean): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ object: "list", data, has_more: hasMore }),
  } as unknown as Response;
}

function wrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

describe("useHostFilesystem", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("stays disabled (no fetch) when hostId or path is null", () => {
    // WHY: the query is lazy — it must not fire until both hostId and path
    // are provided, or the picker would hammer the endpoint while idle.
    const { result } = renderHook(() => useHostFilesystem(null, "/some/path"), {
      wrapper: wrapper(),
    });
    // `fetchStatus: "idle"` is react-query's signal for a disabled query.
    expect(result.current.fetchStatus).toBe("idle");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns a single page when the server reports no more entries", async () => {
    // WHY: the happy path — one page, has_more=false stops the loop, and
    // truncated is false because nothing was cut off.
    fetchMock.mockResolvedValue(pageResponse([entry("a"), entry("b")], false));

    const { result } = renderHook(() => useHostFilesystem("host_1", "/d"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({
      entries: [entry("a"), entry("b")],
      truncated: false,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // The first page must not carry an `after` cursor.
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("after=");
  });

  it("follows has_more pagination using the last entry path as the cursor", async () => {
    // WHY: the endpoint paginates by entry path; each next page must send the
    // previous page's last path as `after`, accumulating all entries.
    fetchMock
      .mockResolvedValueOnce(pageResponse([entry("a"), entry("b")], true))
      .mockResolvedValueOnce(pageResponse([entry("c")], false));

    const { result } = renderHook(() => useHostFilesystem("host_1", "/d"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.entries.map((e) => e.name)).toEqual(["a", "b", "c"]);
    expect(result.current.data?.truncated).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // The second request forwards the first page's last entry path.
    expect(String(fetchMock.mock.calls[1][0])).toContain(`after=${encodeURIComponent("/d/b")}`);
  });

  it("stops on an empty page even if the server still claims has_more", async () => {
    // WHY: defensive empty-page guard — a bad cursor returning [] with
    // has_more=true must not loop forever; the second page ends the fetch.
    fetchMock
      .mockResolvedValueOnce(pageResponse([entry("a")], true))
      .mockResolvedValueOnce(pageResponse([], true));

    const { result } = renderHook(() => useHostFilesystem("host_1", "/d"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.entries.map((e) => e.name)).toEqual(["a"]);
    expect(result.current.data?.truncated).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("throws a FetchError carrying the HTTP status on a non-OK response", async () => {
    // WHY: the picker distinguishes 404 (no such dir) from other failures via
    // err.status, so the thrown error must surface the response status.
    fetchMock.mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({}),
    } as unknown as Response);

    const { result } = renderHook(() => useHostFilesystem("host_1", "/missing"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    const err = result.current.error as (Error & { status?: number }) | null;
    expect(err?.status).toBe(404);
    expect(err?.message).toContain("HTTP 404");
  });
});
