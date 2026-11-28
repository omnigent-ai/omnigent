import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useViewerId } from "@/hooks/useViewerId";

import { useSavedViewScope } from "./useSavedViewScope";

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: vi.fn() }));

beforeEach(() => {
  vi.mocked(useViewerId).mockReturnValue(null);
});

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
  vi.unstubAllGlobals();
});

describe("useSavedViewScope", () => {
  it("offers no scope until the viewer is known", () => {
    expect(renderHook(useSavedViewScope).result.current).toBeNull();
  });

  it.each([
    ["https://example.com:8443", " /proxy/8000/// ", "https://example.com:8443/proxy/8000"],
    ["https://example.com:8443", "/proxy/9000", "https://example.com:8443/proxy/9000"],
    ["https://example.org", undefined, "https://example.org"],
  ])("scopes views to %s and base path %s", (origin, basePath, server) => {
    vi.stubGlobal("location", { origin });
    window.__OMNIGENT_BASE_PATH__ = basePath;
    vi.mocked(useViewerId).mockReturnValue("user-1");

    expect(renderHook(useSavedViewScope).result.current).toEqual({ server, userId: "user-1" });
  });

  it("follows a changed viewer and clears the scope when identity is lost", () => {
    vi.mocked(useViewerId).mockReturnValue("user-1");
    const { result, rerender } = renderHook(useSavedViewScope);
    expect(result.current?.userId).toBe("user-1");

    vi.mocked(useViewerId).mockReturnValue("user-2");
    rerender();
    expect(result.current?.userId).toBe("user-2");

    vi.mocked(useViewerId).mockReturnValue(null);
    rerender();
    expect(result.current).toBeNull();
  });
});
