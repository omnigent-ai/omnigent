// The packaged app's Electron fuses (package.json `build.electronFuses`), run
// with `node --test`. macOS records a Keychain "Always Allow" against the app's
// signature, so any way to run someone else's JavaScript in the signed binary
// hands that grant to every local process.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const packageConfig = require("../package.json");

const INSPECTOR_OVERRIDE = "-c.electronFuses.enableNodeCliInspectArguments=true";

describe("packaged app fuses", () => {
  it("lets a release build run only its own sealed code", () => {
    assert.deepEqual(packageConfig.build.electronFuses, {
      runAsNode: false,
      enableNodeOptionsEnvironmentVariable: false,
      enableNodeCliInspectArguments: false,
      enableEmbeddedAsarIntegrityValidation: true,
      onlyLoadAppFromAsar: true,
      resetAdHocDarwinSignature: true,
    });
    assert.doesNotMatch(packageConfig.scripts["build:mac:release"], /electronFuses/);
  });

  // The dev app has its own identifier, so the release app's grant never covers it.
  it("turns only the main-process debugger back on, and only for the dev app", () => {
    for (const name of ["build", "build:mac", "build:linux", "build:win"]) {
      const script = packageConfig.scripts[name];
      assert.match(script, /-c\.appId=ai\.omnigent\.desktop-dev /, name);
      assert.deepEqual(script.match(/-c\.electronFuses\.\S+/g), [INSPECTOR_OVERRIDE], name);
    }
  });
});
