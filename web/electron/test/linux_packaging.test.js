"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const packageConfig = require("../package.json");

const APP_DIR = path.resolve(__dirname, "..");
const PRODUCT_NAME = packageConfig.build.productName;
const EXECUTABLE = packageConfig.name.toLowerCase();
const INSTALL_DIR = `/opt/${PRODUCT_NAME}`;
const CHROME_SANDBOX = `${INSTALL_DIR}/chrome-sandbox`;

/** Every command the post-install script runs that could modify the host. */
const HOST_TOOLS = [
  "chmod",
  "cp",
  "rm",
  "ln",
  "readlink",
  "update-alternatives",
  "update-mime-database",
  "update-desktop-database",
  "apparmor_parser",
];

/** The deb after-install template electron-builder ships: the configured
 * `deb.afterInstall`, else app-builder-lib's default. */
function debAfterInstallTemplate() {
  const configured = packageConfig.build.deb && packageConfig.build.deb.afterInstall;
  if (configured) return path.resolve(APP_DIR, configured);
  // The default template lives in electron-builder, which CI does not install here.
  let electronBuilderDir;
  try {
    electronBuilderDir = path.dirname(require.resolve("electron-builder/package.json"));
  } catch {
    assert.fail("deb.afterInstall is not configured and electron-builder is not installed");
  }
  const appBuilderLib = path.dirname(
    require.resolve("app-builder-lib/package.json", { paths: [electronBuilderDir] }),
  );
  return path.join(appBuilderLib, "templates", "linux", "after-install.tpl");
}

/** Mirror FpmTarget's macro substitution with the values LinuxPackager derives. */
function renderDebScript(templatePath) {
  const options = {
    executable: EXECUTABLE,
    sanitizedProductName: PRODUCT_NAME,
    productFilename: PRODUCT_NAME,
    ...packageConfig.build.linux,
  };
  return fs.readFileSync(templatePath, "utf8").replace(/\${([a-zA-Z]+)}/g, (_match, name) => {
    if (!(name in options)) throw new Error(`Macro ${name} is not defined`);
    return options[name];
  });
}

function writeStub(dir, name, body) {
  fs.writeFileSync(path.join(dir, name), `#!/bin/bash\n${body}\n`, { mode: 0o755 });
}

/** Run the rendered post-install script as dpkg would, with every host-mutating
 * tool stubbed to record its arguments; returns the recorded calls
 * ("<tool> <args>") in order. */
function runPostInstall({ rootCanUnshare, apparmorEnabled = false }) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-deb-postinst-"));
  try {
    const bin = path.join(tmp, "bin");
    fs.mkdirSync(bin);
    const callLog = path.join(tmp, "calls.log");
    const recordCalls = (name, exitCode = 0) =>
      writeStub(bin, name, `printf '%s\\n' "${name} $*" >> '${callLog}'\nexit ${exitCode}`);
    writeStub(bin, "unshare", rootCanUnshare ? "exit 0" : "exit 1");
    for (const tool of HOST_TOOLS) recordCalls(tool);
    recordCalls("apparmor_status", apparmorEnabled ? 0 : 1);
    const script = path.join(tmp, "after-install.sh");
    fs.writeFileSync(script, renderDebScript(debAfterInstallTemplate()));
    const result = spawnSync("bash", [script], {
      encoding: "utf8",
      env: { PATH: `${bin}:/usr/bin:/bin`, LANG: "C" },
    });
    assert.equal(result.status, 0, result.stderr);
    return fs.existsSync(callLog) ? fs.readFileSync(callLog, "utf8").trim().split("\n") : [];
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

function chromeSandboxModes(calls) {
  return calls
    .filter((call) => call.startsWith("chmod ") && call.endsWith(` ${CHROME_SANDBOX}`))
    .map((call) => call.split(" ")[1]);
}

describe("Linux .deb packaging", { skip: process.platform !== "linux" }, () => {
  it("installs chrome-sandbox setuid root even where the root installer can create user namespaces", () => {
    // Ubuntu with apparmor_restrict_unprivileged_userns=1: root's probe succeeds while the
    // desktop user's Chromium still cannot use the namespace sandbox.
    const calls = runPostInstall({ rootCanUnshare: true });
    assert.equal(chromeSandboxModes(calls).at(-1), "4755", `calls: ${JSON.stringify(calls)}`);
  });

  it("installs chrome-sandbox setuid root where user namespaces are unavailable", () => {
    const calls = runPostInstall({ rootCanUnshare: false });
    assert.equal(chromeSandboxModes(calls).at(-1), "4755", `calls: ${JSON.stringify(calls)}`);
  });

  it("keeps electron-builder's other post-install steps", () => {
    const calls = runPostInstall({ rootCanUnshare: true, apparmorEnabled: true });
    for (const expected of [
      `update-alternatives --install /usr/bin/${EXECUTABLE} ${EXECUTABLE} ${INSTALL_DIR}/${EXECUTABLE} 100`,
      "update-mime-database /usr/share/mime",
      "update-desktop-database /usr/share/applications",
      `apparmor_parser --skip-kernel-load --debug ${INSTALL_DIR}/resources/apparmor-profile`,
      `cp -f ${INSTALL_DIR}/resources/apparmor-profile /etc/apparmor.d/${EXECUTABLE}`,
    ]) {
      assert.ok(calls.includes(expected), `missing ${expected} in ${JSON.stringify(calls)}`);
    }
  });
});
