import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { PreviewService, findPreviewRoot, previewFile } from "./preview.ts";

function project(files: Record<string, string>): string {
  const root = mkdtempSync(join(tmpdir(), "office-preview-"));
  for (const [path, content] of Object.entries(files)) {
    mkdirSync(join(root, path, ".."), { recursive: true });
    writeFileSync(join(root, path), content);
  }
  return root;
}

describe("findPreviewRoot", () => {
  it("prefers built output, then the project root, and finds nothing without an index.html", () => {
    expect(findPreviewRoot(project({ "index.html": "src", "dist/index.html": "built" }))).toBe("dist");
    expect(findPreviewRoot(project({ "index.html": "game" }))).toBe("");
    expect(findPreviewRoot(project({ "package.json": "{}" }))).toBeNull();
  });
});

describe("previewFile", () => {
  it("serves files, folders as index.html, and app routes without an extension", () => {
    const root = project({ "index.html": "home", "src/main.js": "js", "docs/index.html": "docs" });
    expect(previewFile(root, "", "src/main.js")).toMatch(/src\/main\.js$/);
    expect(previewFile(root, "", "")).toMatch(/\/index\.html$/);
    expect(previewFile(root, "", "docs")).toMatch(/docs\/index\.html$/);
    expect(previewFile(root, "", "levels/2")).toMatch(/\/index\.html$/);
    expect(previewFile(root, "", "missing.js")).toBeNull();
  });

  it("never leaves the app's folder, by path or by symlink", () => {
    const outside = project({ "secret.txt": "no" });
    const root = project({ "index.html": "home" });
    symlinkSync(join(outside, "secret.txt"), join(root, "leak.txt"));
    expect(previewFile(root, "", "../secret.txt")).toBeNull();
    expect(previewFile(root, "", "leak.txt")).toBeNull();
  });
});

describe("PreviewService", () => {
  async function serve(service: PreviewService, port: { value: number }) {
    const server = createServer((request, response) => void service.handle(request, response));
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    port.value = (server.address() as AddressInfo).port;
    return server;
  }

  it("serves a local room's app under its id, root paths by cookie, and nothing else", async () => {
    const workspace = project({ "dist/index.html": "<h1>app</h1>", "dist/assets/app.js": "export {}" });
    const port = { value: 0 };
    const service = new PreviewService({
      get port() {
        return port.value;
      },
      resolveRoom: async (projectId) => (projectId === "proj1" ? { workspace, local: true } : projectId === "remote" ? { workspace, local: false } : null),
    });
    const server = await serve(service, port);
    const base = `http://127.0.0.1:${port.value}`;
    try {
      const page = await fetch(`${base}/proj1/`);
      expect(page.status).toBe(200);
      expect(await page.text()).toBe("<h1>app</h1>");
      const cookie = page.headers.get("set-cookie")?.split(";")[0] ?? "";
      const asset = await fetch(`${base}/assets/app.js`, { headers: { cookie } });
      expect(asset.headers.get("content-type")).toContain("text/javascript");
      expect((await fetch(`${base}/assets/app.js`)).status).toBe(404);
      expect((await fetch(`${base}/remote/`)).status).toBe(404);
      expect((await fetch(`${base}/proj1/`, { method: "POST" })).status).toBe(405);
      expect((await fetch(`${base}/proj1`, { redirect: "manual" })).headers.get("location")).toBe("/proj1/");
      expect(await service.entryUrl("proj1")).toEqual({ url: `${base}/proj1/`, folder: "dist" });
      expect(await service.entryUrl("remote")).toBeNull();
    } finally {
      server.close();
    }
  });
});
