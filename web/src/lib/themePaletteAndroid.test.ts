import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expect, it } from "vitest";

it("generates Android colours from the default Omni palette", () => {
  const outputRoot = mkdtempSync(join(tmpdir(), "omnigent-android-palette-"));
  const script = resolve("scripts/generate-theme-palettes.mjs");
  const args = ["--experimental-strip-types", script, "--output-root", outputRoot];
  const lightPath = join(outputRoot, "android/app/src/main/res/values/colors_palette.xml");
  const darkPath = join(outputRoot, "android/app/src/main/res/values-night/colors_palette.xml");

  try {
    for (const directory of [
      "src",
      "android/app/src/main/res/values",
      "android/app/src/main/res/values-night",
    ]) {
      mkdirSync(join(outputRoot, directory), { recursive: true });
    }
    execFileSync(process.execPath, args);
    execFileSync(process.execPath, [...args, "--check"]);

    const light = readFileSync(lightPath, "utf8");
    const dark = readFileSync(darkPath, "utf8");
    expect(light).toContain('<color name="brand_foreground">#27272a</color>');
    expect(dark).toContain('<color name="brand_background">#0e1013</color>');
    expect(dark).toContain('<color name="brand_foreground">#f2f4f5</color>');
    expect(dark).toContain('<color name="brand_border">#27292b</color>');
    expect(light.match(/<color name=/g)).toHaveLength(6);
    expect(dark.match(/<color name=/g)).toHaveLength(6);

    writeFileSync(darkPath, dark.replace("#0e1013", "#0e1014"));
    const stale = spawnSync(process.execPath, [...args, "--check"], { encoding: "utf8" });
    expect(stale.status).toBe(1);
    expect(stale.stderr).toContain(`${darkPath} is stale`);
  } finally {
    rmSync(outputRoot, { recursive: true, force: true });
  }
});
