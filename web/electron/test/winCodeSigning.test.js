const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const pkg = require("../package.json");

// Fields electron-builder recognizes as a Windows Authenticode signing
// mechanism; without one every built executable/installer ships unsigned.
const WIN_SIGNING_FIELDS = [
  "signtoolOptions",
  "azureSignOptions",
  "sign",
  "certificateFile",
  "certificateSubjectName",
  "certificateSha1",
];

describe("Windows code signing", () => {
  it("declares an Authenticode signing mechanism for the Windows build", () => {
    const win = pkg.build.win ?? {};
    const declared = WIN_SIGNING_FIELDS.filter((field) => field in win);
    assert.ok(
      declared.length > 0,
      `build.win declares no code-signing mechanism (expected one of: ${WIN_SIGNING_FIELDS.join(", ")}); ` +
        "every Windows executable and NSIS installer built from this configuration ships " +
        "without an Authenticode signature and is refused by Windows devices that " +
        "require signed installers",
    );
  });

  it("refuses to release an unsigned Windows installer", () => {
    const releaseScript = pkg.scripts["build:win:release"] ?? "";
    const enforced =
      pkg.build.forceCodeSigning === true ||
      (pkg.build.win ?? {}).forceCodeSigning === true ||
      releaseScript.includes("forceCodeSigning");
    assert.ok(
      enforced,
      "no Windows release build enforces code signing (the analog of build:mac:release's " +
        "notarization gate); a release build silently produces an unsigned installer " +
        "instead of aborting when signing material is unavailable",
    );
  });
});
