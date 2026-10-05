import { describe, expect, it, vi } from "vitest";
import { DESIGN_SYSTEM_POINTER, serializeDesignSystemPointer } from "@/lib/designSystem";
import { listFixtureDir, readFixtureFile } from "@/test/designSystemFixture";
import { DESIGN_KIT_DIR, type KitFile } from "./codeViewerHelpers";
import {
  DS_OWNER_ONLY,
  DS_UNREADABLE,
  loadDeckBranding,
  NO_BRANDING,
  type BrandingDeps,
} from "./deckBranding";

const FOLDER = "/Users/me/brand/fixture";
const DECK = '<section><img src="ds:assets/logo.svg"></section>';
const text = (content: string): KitFile => ({ encoding: "utf-8", content, bytes: content.length });
const pointer = (kind: "full" | "skill", path = FOLDER) =>
  text(serializeDesignSystemPointer({ path, kind, name: "Fixture Brand" }));

/** Workspace files by path, plus the fixture under `root` (absolute or relative). */
function deps(
  files: Record<string, KitFile>,
  opts: { owner?: boolean; root?: string; fail?: string } = {},
): BrandingDeps & {
  read: ReturnType<typeof vi.fn>;
  isOwner: ReturnType<typeof vi.fn>;
  list?: ReturnType<typeof vi.fn>;
} {
  const root = `${opts.root ?? FOLDER}/`;
  return {
    read: vi.fn(async (path: string) => {
      if (path.startsWith(root)) {
        if (opts.fail) throw new Error(opts.fail);
        return readFixtureFile(path.slice(root.length));
      }
      return files[path] ?? null;
    }),
    isOwner: vi.fn(async () => opts.owner ?? true),
    onDesignSystem: vi.fn(),
    list: vi.fn(async (dir: string) =>
      dir.startsWith(root) ? listFixtureDir(dir.slice(root.length)) : [],
    ),
  };
}

describe("loadDeckBranding", () => {
  it("uses the kit when there is no pointer", async () => {
    const d = deps({ [`${DESIGN_KIT_DIR}/kit.json`]: text('{"name":"Acme"}') });
    const branding = await loadDeckBranding(DECK, d);
    expect(branding.badge).toEqual({ kind: "kit", name: "Acme" });
    expect(branding.kitStyle).toContain("data-omnigent-kit");
    expect(branding.content).toBeNull();
    expect(d.isOwner).not.toHaveBeenCalled();
  });

  it("passes sections: false to the kit for wireframes", async () => {
    const kit = text(
      '{"name":"Acme","colors":{"background":"#fff"},"fonts":{"body":{"family":"Inter"}}}',
    );
    const d = deps({ [`${DESIGN_KIT_DIR}/kit.json`]: kit });
    const branding = await loadDeckBranding(DECK, d, { sections: false });
    expect(branding.kitStyle).toContain("--kit-background:#fff");
    expect(branding.kitStyle).not.toContain("body>section");
    expect(branding.kitStyle).not.toContain("!important");
    expect((await loadDeckBranding(DECK, d)).kitStyle).toContain("body>section");
  });

  it("injects a full system the same way for wireframes", async () => {
    const d = deps({ [DESIGN_SYSTEM_POINTER]: pointer("full") });
    expect(await loadDeckBranding(DECK, d, { sections: false })).toEqual({
      ...(await loadDeckBranding(DECK, d)),
      brandWarnings: null,
    });
  });

  describe("brand warnings", () => {
    const BRAND_DECK = `<style>.title h1{color:#0B5FFF;font-size:64px}h2{color:#ff0000}</style>
<section class="title"><h1 style="font-family:Comic Sans">Hi #123456</h1></section>`;
    const full = () => deps({ [DESIGN_SYSTEM_POINTER]: pointer("full") });

    it("flags raw values a full system's templates do not use", async () => {
      const d = full();
      expect((await loadDeckBranding(BRAND_DECK, d)).brandWarnings).toEqual([
        { value: "#ff0000", property: "color", where: "<style> h2" },
        { value: "Comic Sans", property: "font-family", where: "slide 1 <h1>" },
      ]);
      expect(d.read).toHaveBeenCalledWith(`${FOLDER}/_adherence.oxlintrc.json`);
      expect(d.list).toHaveBeenCalledWith(`${FOLDER}/templates`);
      expect(d.read).toHaveBeenCalledWith(`${FOLDER}/templates/title.html`);
    });

    it("reads an imported system's rules and templates through the workspace", async () => {
      const root = ".omnigent/design-system";
      const d = deps({ [DESIGN_SYSTEM_POINTER]: pointer("full", root) }, { root });
      expect((await loadDeckBranding(BRAND_DECK, d)).brandWarnings).toHaveLength(2);
      expect(d.read).toHaveBeenCalledWith(`${root}/templates/title.html`);
    });

    it("is empty, not null, for a clean deck", async () => {
      expect((await loadDeckBranding(DECK, full())).brandWarnings).toEqual([]);
    });

    it("skips wireframes without reading the rules", async () => {
      const d = full();
      expect((await loadDeckBranding(BRAND_DECK, d, { sections: false })).brandWarnings).toBeNull();
      expect(d.read).not.toHaveBeenCalledWith(`${FOLDER}/_adherence.oxlintrc.json`);
      expect(d.list).not.toHaveBeenCalled();
    });

    it("has no warnings for kits and skill-only systems", async () => {
      const kit = deps({ [`${DESIGN_KIT_DIR}/kit.json`]: text('{"name":"Acme"}') });
      const skill = deps({ [DESIGN_SYSTEM_POINTER]: pointer("skill") });
      expect((await loadDeckBranding(BRAND_DECK, kit)).brandWarnings).toBeNull();
      expect((await loadDeckBranding(BRAND_DECK, skill)).brandWarnings).toBeNull();
    });

    it.each([
      ["no listing", (d: ReturnType<typeof full>) => delete d.list],
      [
        "a failed listing",
        (d: ReturnType<typeof full>) => d.list!.mockRejectedValue(new Error("500")),
      ],
      [
        "an oversized rules file",
        (d: ReturnType<typeof full>) => {
          const read = d.read.getMockImplementation() as BrandingDeps["read"];
          const big: KitFile = { encoding: "utf-8", content: "{}", bytes: 3 * 1024 * 1024 };
          d.read.mockImplementation(async (p: string) =>
            p.endsWith("_adherence.oxlintrc.json") ? big : read(p),
          );
        },
      ],
    ])("shows no warnings, and still brands, with %s", async (_label, breakIt) => {
      const d = full();
      breakIt(d);
      const branding = await loadDeckBranding(BRAND_DECK, d);
      expect(branding.brandWarnings).toBeNull();
      expect(branding.notice).toBeNull();
      expect(branding.systemStyle).toContain("--fx-primary");
    });
  });

  it("names a kit failure as before", async () => {
    const branding = await loadDeckBranding(
      DECK,
      deps({ [`${DESIGN_KIT_DIR}/kit.json`]: text("{") }),
    );
    expect(branding.notice).toBe("Design kit not applied: kit.json is not valid JSON");
  });

  it("is unbranded with neither a pointer nor a kit", async () => {
    expect(await loadDeckBranding(DECK, deps({}))).toEqual(NO_BRANDING);
  });

  it("shows a skill-only system's name and injects nothing, ignoring the kit", async () => {
    const d = deps({
      [DESIGN_SYSTEM_POINTER]: pointer("skill"),
      [`${DESIGN_KIT_DIR}/kit.json`]: text('{"name":"Acme"}'),
    });
    expect(await loadDeckBranding(DECK, d)).toEqual({
      ...NO_BRANDING,
      badge: { kind: "system", name: "Fixture Brand" },
    });
    expect(d.read).toHaveBeenCalledTimes(1);
  });

  it("injects a full system and rewrites ds: assets", async () => {
    const d = deps({ [DESIGN_SYSTEM_POINTER]: pointer("full") });
    const branding = await loadDeckBranding(DECK, d);
    expect(branding.notice).toBeNull();
    expect(branding.badge).toEqual({ kind: "system", name: "Fixture Brand" });
    expect(branding.kitStyle).toBe("");
    expect(branding.systemStyle).toContain("--fx-primary");
    expect(branding.content).toMatch(/^<section><img src="data:image\/svg\+xml;base64,/);
    expect(d.read).toHaveBeenCalledWith(`${FOLDER}/colors_and_type.css`);
    expect(d.isOwner).toHaveBeenCalled();
    expect(d.onDesignSystem).toHaveBeenCalledTimes(1);
  });

  it("reads a relative system through the workspace without the owner check", async () => {
    const root = ".omnigent/design-system";
    const d = deps({ [DESIGN_SYSTEM_POINTER]: pointer("full", root) }, { root, owner: false });
    const branding = await loadDeckBranding(DECK, d);
    expect(branding.systemStyle).toContain("--fx-primary");
    expect(d.read).toHaveBeenCalledWith(`${root}/colors_and_type.css`);
    expect(d.isOwner).not.toHaveBeenCalled();
  });

  it("tells a viewer who is not the owner, without any absolute read", async () => {
    const d = deps({ [DESIGN_SYSTEM_POINTER]: pointer("full") }, { owner: false });
    expect(await loadDeckBranding(DECK, d)).toEqual({ ...NO_BRANDING, notice: DS_OWNER_ONLY });
    expect(d.read).toHaveBeenCalledTimes(1);
    expect(d.onDesignSystem).not.toHaveBeenCalled();
  });

  it("asks to import when the owner's session cannot read the folder", async () => {
    const d = deps({ [DESIGN_SYSTEM_POINTER]: pointer("full") }, { fail: "403 Forbidden" });
    expect((await loadDeckBranding(DECK, d)).notice).toBe(DS_UNREADABLE);
  });

  it.each([
    ["an invalid pointer", { [DESIGN_SYSTEM_POINTER]: text('{"path":"/x","kind":"kit"}') }, {}],
    ["a failed read", { [DESIGN_SYSTEM_POINTER]: pointer("full") }, { fail: "500 Server Error" }],
  ])("names the reason for %s", async (_label, files, opts) => {
    const branding = await loadDeckBranding(DECK, deps(files, opts));
    expect(branding.notice).toMatch(/^Design system not applied: /);
    expect(branding.systemStyle).toBe("");
    expect(branding.content).toBeNull();
  });

  it("rejects a ds: path that escapes the folder", async () => {
    const branding = await loadDeckBranding(
      '<img src="ds:../secret.png">',
      deps({ [DESIGN_SYSTEM_POINTER]: pointer("full") }),
    );
    expect(branding.notice).toBe(
      "Design system not applied: ds:../secret.png must be a relative path inside the design system",
    );
  });

  it("names a failed owner check", async () => {
    const d = deps({ [DESIGN_SYSTEM_POINTER]: pointer("full") });
    d.isOwner.mockRejectedValue(new Error("502 Bad Gateway"));
    expect((await loadDeckBranding(DECK, d)).notice).toBe(
      "Design system not applied: 502 Bad Gateway",
    );
  });
});
