import { describe, expect, it } from "vitest";
import { isLocalServerOrigin } from "./serverOrigin";

describe("serverOrigin", () => {
  it("classifies loopback origins as local", () => {
    expect(isLocalServerOrigin("http://localhost:6767")).toBe(true);
    expect(isLocalServerOrigin("http://127.0.0.1:6767")).toBe(true);
    expect(isLocalServerOrigin("http://0.0.0.0:6767")).toBe(true);
    expect(isLocalServerOrigin("http://[::1]:6767")).toBe(true);
  });

  it("does not classify public origins as local", () => {
    expect(isLocalServerOrigin("https://app.example.com")).toBe(false);
    expect(isLocalServerOrigin("https://192.168.1.50:6767")).toBe(false);
    expect(isLocalServerOrigin("not a url")).toBe(false);
  });
});
