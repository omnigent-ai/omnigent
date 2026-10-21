import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, it } from "vitest";

it("generates Android colours from the default Omni palette", () => {
  execFileSync(
    process.execPath,
    ["--experimental-strip-types", "scripts/generate-theme-palettes.mjs", "--check"],
    {
      cwd: process.cwd(),
    },
  );

  const light = readFileSync(resolve("android/app/src/main/res/values/colors_palette.xml"), "utf8");
  const dark = readFileSync(
    resolve("android/app/src/main/res/values-night/colors_palette.xml"),
    "utf8",
  );

  expect(light).toContain('<color name="brand_foreground">#27272a</color>');
  expect(dark).toContain('<color name="brand_background">#0e1013</color>');
  expect(dark).toContain('<color name="brand_foreground">#f2f4f5</color>');
  expect(dark).toContain('<color name="brand_border">#27292b</color>');
  expect(light.match(/<color name=/g)).toHaveLength(6);
  expect(dark.match(/<color name=/g)).toHaveLength(6);
});
