// Tests for the electron-builder afterPack hook (build/afterPack.js), run with
// `node --test`. The hook must stop a macOS build that would ship without the
// native Keychain binding for its CPU.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const afterPack = require("../build/afterPack");

const X64 = 1;
const ARM64 = 3;
const UNIVERSAL = 4;

/** A packed `Omnigent.app` holding the given Keychain bindings; returns the pack context. */
function packedApp(root, arch, bindings, platform = "darwin") {
  const unpacked = path.join(
    root,
    "Omnigent.app",
    "Contents",
    "Resources",
    "app.asar.unpacked",
    "node_modules",
    "@napi-rs",
  );
  fs.mkdirSync(unpacked, { recursive: true });
  for (const cpu of bindings) {
    const directory = path.join(unpacked, `keyring-darwin-${cpu}`);
    fs.mkdirSync(directory);
    fs.writeFileSync(path.join(directory, `keyring.darwin-${cpu}.node`), "");
  }
  return {
    electronPlatformName: platform,
    arch,
    appOutDir: root,
    packager: { appInfo: { productFilename: "Omnigent" } },
  };
}

describe("afterPack", () => {
  for (const [name, arch, bindings, missing] of [
    ["an arm64 app without its binding", ARM64, ["x64"], /keyring\.darwin-arm64\.node/],
    ["an x64 app without its binding", X64, ["arm64"], /keyring\.darwin-x64\.node/],
    ["a universal app with one binding", UNIVERSAL, ["arm64"], /keyring\.darwin-x64\.node/],
  ]) {
    it(`refuses ${name} and says how to install it`, async () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-after-pack-"));
      try {
        await assert.rejects(afterPack(packedApp(root, arch, bindings)), (err) => {
          assert.match(err.message, missing);
          assert.match(err.message, /--cpu x64 --cpu arm64/);
          return true;
        });
        // Nothing was added to an app that is about to be thrown away.
        assert.equal(
          fs.existsSync(path.join(root, "Omnigent.app", "Contents", "Resources", "Assets.car")),
          false,
        );
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }

  it("accepts an app that carries the binding for its CPU", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-after-pack-"));
    try {
      await afterPack(packedApp(root, ARM64, ["x64", "arm64"]));
      assert.equal(
        fs.existsSync(path.join(root, "Omnigent.app", "Contents", "Resources", "Assets.car")),
        true,
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it("leaves other platforms alone", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-after-pack-"));
    try {
      await afterPack(packedApp(root, X64, [], "linux"));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});
