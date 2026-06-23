import { afterEach, describe, expect, it } from "vitest";

import { getBasePath, stripBasePath, withBasePath } from "./basePath";

declare global {
  interface Window {
    __OMNIGENT_BASE_PATH__?: string;
  }
}

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
});

function setBase(value: string | undefined): void {
  if (value === undefined) {
    delete window.__OMNIGENT_BASE_PATH__;
  } else {
    window.__OMNIGENT_BASE_PATH__ = value;
  }
}

describe("getBasePath", () => {
  it("returns empty string when unset", () => {
    setBase(undefined);
    expect(getBasePath()).toBe("");
  });

  it("treats empty string and bare slash as root (empty)", () => {
    setBase("");
    expect(getBasePath()).toBe("");
    setBase("/");
    expect(getBasePath()).toBe("");
  });

  it("returns a normalized leading-slash, no-trailing-slash path", () => {
    setBase("/proxy/6767");
    expect(getBasePath()).toBe("/proxy/6767");
  });

  it("strips a trailing slash", () => {
    setBase("/proxy/6767/");
    expect(getBasePath()).toBe("/proxy/6767");
  });

  it("adds a missing leading slash", () => {
    setBase("proxy/6767");
    expect(getBasePath()).toBe("/proxy/6767");
  });

  it("trims surrounding whitespace", () => {
    setBase("  /proxy/6767  ");
    expect(getBasePath()).toBe("/proxy/6767");
  });
});

describe("withBasePath", () => {
  it("returns the path unchanged when no base is set", () => {
    setBase(undefined);
    expect(withBasePath("/v1/sessions")).toBe("/v1/sessions");
  });

  it("prepends the base to an app-absolute path", () => {
    setBase("/proxy/6767");
    expect(withBasePath("/v1/sessions")).toBe("/proxy/6767/v1/sessions");
  });

  it("is idempotent for an already-prefixed path", () => {
    setBase("/proxy/6767");
    expect(withBasePath("/proxy/6767/v1/sessions")).toBe("/proxy/6767/v1/sessions");
  });

  it("returns the base itself when the path equals the base", () => {
    setBase("/proxy/6767");
    expect(withBasePath("/proxy/6767")).toBe("/proxy/6767");
  });

  it("leaves non-absolute paths (full URLs, blob:) untouched", () => {
    setBase("/proxy/6767");
    expect(withBasePath("https://example.com/x")).toBe("https://example.com/x");
    expect(withBasePath("blob:abc")).toBe("blob:abc");
  });
});

describe("stripBasePath", () => {
  it("returns the pathname unchanged when no base is set", () => {
    setBase(undefined);
    expect(stripBasePath("/login")).toBe("/login");
  });

  it("removes the base prefix from a pathname", () => {
    setBase("/proxy/6767");
    expect(stripBasePath("/proxy/6767/login")).toBe("/login");
  });

  it("returns root when the pathname equals the base", () => {
    setBase("/proxy/6767");
    expect(stripBasePath("/proxy/6767")).toBe("/");
  });

  it("leaves a pathname not under the base untouched", () => {
    setBase("/proxy/6767");
    expect(stripBasePath("/login")).toBe("/login");
  });
});
