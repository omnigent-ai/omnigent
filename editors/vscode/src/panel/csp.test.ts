/**
 * CSP unit tests for the iframe host page. The host page needs exactly three
 * directives; everything Monaco/embed-related (script-src/connect-src/worker-src/
 * img-src) is intentionally absent because the framed app is governed by the
 * server's own headers, not this policy.
 */
import { describe, it, expect } from "vitest";
import { buildCsp } from "./csp";

describe("buildCsp", () => {
  const base = { serverOrigin: "http://127.0.0.1:6767", nonce: "test-nonce-abc" };

  it("contains default-src 'none'", () => {
    expect(buildCsp(base)).toContain("default-src 'none'");
  });

  it("style-src uses the nonce", () => {
    const csp = buildCsp(base);
    const styleDirective = csp.split(";").find((d) => d.trim().startsWith("style-src")) ?? "";
    expect(styleDirective).toContain("'nonce-test-nonce-abc'");
  });

  it("frame-src allows the server origin", () => {
    const csp = buildCsp(base);
    const frameDirective = csp.split(";").find((d) => d.trim().startsWith("frame-src")) ?? "";
    expect(frameDirective).toContain("http://127.0.0.1:6767");
    expect(frameDirective).not.toContain("'none'");
  });

  it("includes cspSource in style-src when provided", () => {
    const csp = buildCsp({ ...base, cspSource: "vscode-resource:" });
    const styleDirective = csp.split(";").find((d) => d.trim().startsWith("style-src")) ?? "";
    expect(styleDirective).toContain("vscode-resource:");
  });

  it("does NOT emit script-src / connect-src / worker-src / img-src (iframe host only)", () => {
    const csp = buildCsp(base);
    expect(csp).not.toContain("script-src");
    expect(csp).not.toContain("connect-src");
    expect(csp).not.toContain("worker-src");
    expect(csp).not.toContain("img-src");
    expect(csp).not.toContain("wasm-unsafe-eval");
  });

  it("never references a token", () => {
    expect(buildCsp(base).toLowerCase()).not.toContain("token");
  });
});
