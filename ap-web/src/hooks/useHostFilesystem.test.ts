// Unit tests for the URL builder used by the host filesystem hook.
// Pins the path encoding so a regression doesn't silently produce
// URLs that the FastAPI route rejects (404) or that double-encode
// segments (resulting in literal "%2F" reaching the host).

import { describe, expect, it } from "vitest";

import { buildHostFilesystemUrl } from "./useHostFilesystem";

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
