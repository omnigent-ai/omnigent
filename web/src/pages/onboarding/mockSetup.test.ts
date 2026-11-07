import { describe, expect, it, vi } from "vitest";
import { maybeMockSetup } from "./mockSetup";

const params = (q: string) => new URLSearchParams(q);

describe("maybeMockSetup", () => {
  it("returns null without ?mock=1", () => {
    expect(maybeMockSetup(params("managed=https://x.example.com"))).toBeNull();
  });

  it("parses the four-variant params", () => {
    const setup = maybeMockSetup(
      params(
        "mock=1&installed=1&managed=https://a.example.com,https://b.example.com&recents=https://r.example.com",
      ),
    );
    expect(setup).not.toBeNull();
    expect(setup?.managedServers).toEqual(["https://a.example.com", "https://b.example.com"]);
    expect(setup?.recentServers).toEqual(["https://r.example.com"]);
    expect(setup?.installed).toBe(true);
  });

  it("defaults to a new user with empty lists", () => {
    const setup = maybeMockSetup(params("mock=1"));
    expect(setup?.installed).toBe(false);
    expect(setup?.managedServers).toEqual([]);
    expect(setup?.recentServers).toEqual([]);
  });
});

describe("maybeMockSetup clipboard", () => {
  it("writes to the real browser clipboard and rejects when denied", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await maybeMockSetup(params("mock=1"))?.onCopy("hello");
    expect(writeText).toHaveBeenCalledWith("hello");
    await expect(maybeMockSetup(params("mock=1&clipboard=deny"))?.onCopy("x")).rejects.toThrow();
  });
});
