const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const packageConfig = require("../package.json");

describe("Windows packaging", () => {
  it("declares Authenticode signing options for the Windows build", () => {
    // signtoolOptions wires electron-builder's Windows signing path: with
    // signing material present (WIN_CSC_LINK / WIN_CSC_KEY_PASSWORD) the
    // executable and NSIS installer are signed; without it a dev build
    // still succeeds unsigned.
    const signtoolOptions = packageConfig.build.win.signtoolOptions;
    assert.ok(signtoolOptions, "build.win must configure signtoolOptions");
    assert.deepEqual(signtoolOptions.signingHashAlgorithms, ["sha256"]);
  });

  it("forces code signing on release builds so they cannot ship unsigned", () => {
    // Mirrors build:mac:release's fail-loud behavior: a release installer
    // must abort when signing material is missing, never silently ship an
    // unsigned artifact that managed Windows devices refuse to install.
    const releaseScript = packageConfig.scripts["build:win:release"];
    assert.ok(releaseScript, "a build:win:release script must exist");
    assert.match(releaseScript, /-c\.win\.forceCodeSigning=true/);
  });

  it("builds the SPA overlay before a release build", () => {
    assert.equal(packageConfig.scripts["prebuild:win:release"], "pnpm run build:overlay");
  });
});
