const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const packageConfig = require("../package.json");

// electron-builder silently skips Authenticode signing when no certificate is
// supplied unless forceCodeSigning makes the build abort instead.
describe("Windows packaging", () => {
  it("signs with SHA-256 Authenticode when signing material is present", () => {
    assert.deepEqual(packageConfig.build.win.signtoolOptions?.signingHashAlgorithms, ["sha256"]);
  });

  it("lets a dev build succeed without signing material", () => {
    assert.notEqual(packageConfig.build.forceCodeSigning, true);
    assert.notEqual(packageConfig.build.win.forceCodeSigning, true);
    assert.doesNotMatch(packageConfig.scripts["build:win"], /forceCodeSigning/);
  });

  it("release builds refuse to ship an unsigned installer", () => {
    const releaseScript = packageConfig.scripts["build:win:release"];
    assert.ok(
      releaseScript,
      "no build:win:release script: the only Windows build is the unsigned dev build",
    );
    assert.match(
      releaseScript,
      /-c\.win\.forceCodeSigning=true/,
      "the Windows release build does not force code signing, so electron-builder emits an unsigned NSIS installer when no certificate is supplied",
    );
  });

  it("release builds keep the production identity and bundle the overlay", () => {
    assert.doesNotMatch(
      packageConfig.scripts["build:win:release"],
      /desktop-dev|dist-dev|omnigentBuild=dev/,
    );
    assert.equal(packageConfig.scripts["prebuild:win:release"], "pnpm run build:overlay");
  });
});
