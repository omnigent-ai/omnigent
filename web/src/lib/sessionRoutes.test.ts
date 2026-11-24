import { afterEach, describe, expect, it } from "vitest";

import { sessionIdAtPath } from "./sessionRoutes";

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
});

describe("sessionIdAtPath", () => {
  it("finds the session a chat or Canvas route opens, with or without a trailing slash", () => {
    expect(sessionIdAtPath("/c/abc")).toBe("abc");
    expect(sessionIdAtPath("/c/abc/")).toBe("abc");
    expect(sessionIdAtPath("/canvas/c/abc")).toBe("abc");
    expect(sessionIdAtPath("/")).toBeNull();
    expect(sessionIdAtPath("/extensions/demo/c/abc")).toBeNull();
  });

  it("decodes the path as the router does", () => {
    expect(sessionIdAtPath("/canvas/c/%31%32%33")).toBe("123");
    expect(sessionIdAtPath("/c/a%20b")).toBe("a b");
  });

  it("reads a path under the server's base path, and nothing outside it", () => {
    window.__OMNIGENT_BASE_PATH__ = "/omnigent";
    expect(sessionIdAtPath("/omnigent/c/abc")).toBe("abc");
    expect(sessionIdAtPath("/c/abc")).toBeNull();
    expect(sessionIdAtPath("/omnigentx/c/abc")).toBeNull();
  });

  it("matches the routes as an embedding host mounts them", () => {
    const rebase = (path: string) => `/mount${path}`;
    expect(sessionIdAtPath("/mount/c/abc", rebase)).toBe("abc");
    expect(sessionIdAtPath("/c/abc", rebase)).toBeNull();
  });
});
