"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { readMacAppIcon } = require("../src/macAppIcon");

for (const filename of ["Editor", "Editor.icns"]) {
  test(`extracts the bundle's ${filename} icon and removes temporary files`, async () => {
    let output;
    const icon = Buffer.from("png bytes");
    const run = async (command, args, options) => {
      assert.equal(options.timeout, 3000);
      if (command === "/usr/bin/plutil") {
        assert.deepEqual(args, [
          "-extract",
          "CFBundleIconFile",
          "raw",
          "-o",
          "-",
          "/Applications/My Editor.app/Contents/Info.plist",
        ]);
        return { stdout: filename + "\n" };
      }
      assert.equal(command, "/usr/bin/sips");
      output = args.at(-1);
      assert.deepEqual(args.slice(0, -1), [
        "-s",
        "format",
        "png",
        "-Z",
        "32",
        "/Applications/My Editor.app/Contents/Resources/Editor.icns",
        "--out",
      ]);
      await fs.writeFile(output, icon);
      return { stdout: "" };
    };
    assert.equal(
      await readMacAppIcon("/Applications/My Editor.app", { run }),
      `data:image/png;base64,${icon.toString("base64")}`,
    );
    await assert.rejects(fs.stat(path.dirname(output)), { code: "ENOENT" });
  });
}

test("missing or unreadable artwork falls back and cleans failed conversions", async () => {
  let output;
  const run = async (command, args) => {
    if (command === "/usr/bin/plutil") return { stdout: "Editor.icns" };
    output = args.at(-1);
    throw new Error("unreadable icon");
  };
  assert.equal(await readMacAppIcon("/Applications/Editor.app", { run }), null);
  await assert.rejects(fs.stat(path.dirname(output)), { code: "ENOENT" });
  assert.equal(
    await readMacAppIcon("/missing.app", {
      run: async () => {
        throw new Error("missing bundle");
      },
    }),
    null,
  );
});

for (const filename of ["", "..", "../secret", "/absolute/icon.icns"]) {
  test(`does not read invalid bundle icon filename ${JSON.stringify(filename)}`, async () => {
    const commands = [];
    const run = async (command) => {
      commands.push(command);
      return { stdout: filename };
    };
    assert.equal(await readMacAppIcon("/Applications/Editor.app", { run }), null);
    assert.deepEqual(commands, ["/usr/bin/plutil"]);
  });
}
