import { describe, expect, it } from "vitest";
import { isRemoteVideoUrl, isVideoFile } from "./video";

describe("video detection", () => {
  it.each([
    "demo.mp4",
    "DEMO.WEBM",
    "demo.mov",
    "demo.m4v",
    "demo.ogv",
    "https://example.com/demo.mp4?token=abc#t=5",
  ])("recognizes %s", (path) => {
    expect(isVideoFile(path)).toBe(true);
  });
  it("recognizes video MIME types and rejects mislabeled code", () => {
    expect(isVideoFile("recording", "video/mp4; codecs=avc1")).toBe(true);
    expect(isVideoFile("demo.webm", "application/octet-stream")).toBe(true);
    expect(isVideoFile("source.ts", "video/mp2t")).toBe(false);
    expect(isVideoFile("demo.mp4", "text/plain")).toBe(false);
  });
  it("permits direct HTTP links, including signed URLs, and excludes unsafe schemes", () => {
    expect(isRemoteVideoUrl("https://example.com/demo.mp4?signature=abc")).toBe(true);
    expect(isRemoteVideoUrl("//example.com/demo.webm")).toBe(true);
    for (const value of [
      "javascript:demo.mp4",
      "data:video/mp4;base64,AA",
      "file:///demo.mp4",
      "demo.mp4",
      "https://example.com/page",
      "https://example.mp4",
    ]) {
      expect(isRemoteVideoUrl(value)).toBe(false);
    }
  });
});
