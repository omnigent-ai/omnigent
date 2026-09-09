import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type FontCatalogEntry, getFontById } from "./fontCatalog";
import { resetFontLoaderForTests, loadFont, loadFontByFamily } from "./webFontLoader";

// jsdom has no FontFaceSet; stub document.fonts.load so readiness resolves and
// we can count how often a given family was requested.
let loadCalls: string[] = [];

beforeEach(() => {
  loadCalls = [];
  Object.defineProperty(document, "fonts", {
    configurable: true,
    value: {
      load: vi.fn((spec: string) => {
        loadCalls.push(spec);
        return Promise.resolve([]);
      }),
    },
  });
});

afterEach(() => {
  resetFontLoaderForTests();
  for (const node of document.querySelectorAll("[data-omnigent-font]")) node.remove();
  vi.restoreAllMocks();
});

function styleNodes(id: string): number {
  return document.querySelectorAll(`[data-omnigent-font="${id}"]`).length;
}

describe("webFontLoader — google-css2", () => {
  it("injects a stylesheet link once and awaits readiness", async () => {
    const inter = getFontById("inter") as FontCatalogEntry;
    await loadFont(inter);

    const link = document.querySelector<HTMLLinkElement>(`link[data-omnigent-font="inter"]`);
    expect(link).not.toBeNull();
    expect(link?.rel).toBe("stylesheet");
    expect(link?.href).toBe(inter.cssUrl);
    // Readiness awaited via document.fonts.load('16px "<family>"').
    expect(loadCalls).toContain(`16px "Inter"`);
  });

  it("never injects the same stylesheet twice", async () => {
    const inter = getFontById("inter") as FontCatalogEntry;
    await loadFont(inter);
    await loadFont(inter);
    expect(styleNodes("inter")).toBe(1);
  });

  it("shares one in-flight promise for concurrent loads", async () => {
    const roboto = getFontById("roboto") as FontCatalogEntry;
    const a = loadFont(roboto);
    const b = loadFont(roboto);
    expect(a).toBe(b);
    await Promise.all([a, b]);
    expect(styleNodes("roboto")).toBe(1);
    // A single readiness await, not one per call.
    expect(loadCalls.filter((s) => s === `16px "Roboto"`).length).toBe(1);
  });
});

describe("webFontLoader — self-hosted", () => {
  it("injects @font-face rules once for a Nerd Font", async () => {
    const nerd = getFontById("jetbrainsmono-nerd-font-mono") as FontCatalogEntry;
    await loadFont(nerd);

    const style = document.querySelector<HTMLStyleElement>(
      `style[data-omnigent-font="jetbrainsmono-nerd-font-mono"]`,
    );
    expect(style).not.toBeNull();
    expect(style?.textContent).toContain("@font-face");
    expect(style?.textContent).toContain(`font-family: 'JetBrainsMono Nerd Font Mono'`);
    expect(style?.textContent).toContain(nerd.faces?.[0].url ?? "MISSING");
    expect(loadCalls).toContain(`16px "JetBrainsMono Nerd Font Mono"`);
  });

  it("injects multiple faces for a multi-weight self-hosted font", async () => {
    const cascadia = getFontById("cascadia-code") as FontCatalogEntry;
    await loadFont(cascadia);
    const style = document.querySelector(`style[data-omnigent-font="cascadia-code"]`);
    const faceCount = style?.textContent?.match(/@font-face/g)?.length ?? 0;
    expect(faceCount).toBe(cascadia.faces?.length);
  });
});

describe("webFontLoader — no-ops", () => {
  it("does not fetch a bundled font", async () => {
    const geist = getFontById("geist-mono") as FontCatalogEntry;
    await loadFont(geist);
    expect(styleNodes("geist-mono")).toBe(0);
    expect(loadCalls.length).toBe(0);
  });

  it("does not fetch the empty system-default family", async () => {
    const system = getFontById("system-ui") as FontCatalogEntry;
    await loadFont(system);
    expect(loadCalls.length).toBe(0);
  });
});

describe("webFontLoader — loadFontByFamily bridge", () => {
  it("loads a catalog family typed as a bare name", async () => {
    const { entry, ready } = loadFontByFamily("Fira Code");
    expect(entry?.id).toBe("fira-code");
    await ready;
    expect(styleNodes("fira-code")).toBe(1);
    expect(loadCalls).toContain(`16px "Fira Code"`);
  });

  it("resolves without loading for a non-catalog family", async () => {
    const { entry, ready } = loadFontByFamily("Comic Sans MS");
    expect(entry).toBeUndefined();
    await ready;
    expect(loadCalls.length).toBe(0);
  });

  it("resolves without loading for an empty family", async () => {
    const { entry, ready } = loadFontByFamily("");
    expect(entry).toBeUndefined();
    await ready;
    expect(loadCalls.length).toBe(0);
  });
});
