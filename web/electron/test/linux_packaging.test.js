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

/** Host commands the post-install script looks up on PATH. */
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

const DEB_AFTER_INSTALL = path.resolve(APP_DIR, packageConfig.build.deb.afterInstall);

/** electron-builder's default deb after-install template, or null when it is not installed. */
function upstreamAfterInstallTemplate() {
  try {
    const electronBuilderDir = path.dirname(require.resolve("electron-builder/package.json"));
    const appBuilderLib = path.dirname(
      require.resolve("app-builder-lib/package.json", { paths: [electronBuilderDir] }),
    );
    return path.join(appBuilderLib, "templates", "linux", "after-install.tpl");
  } catch (error) {
    if (error.code === "MODULE_NOT_FOUND") return null;
    throw error;
  }
}

/** Expand the template's macros as FpmTarget does, using the release build's names. */
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

/** Run the rendered script with host tools stubbed; return the recorded calls in order. */
function runPostInstall({ apparmorEnabled = false } = {}) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-deb-postinst-"));
  try {
    const bin = path.join(tmp, "bin");
    fs.mkdirSync(bin);
    const callLog = path.join(tmp, "calls.log");
    const recordCalls = (name, exitCode = 0) =>
      writeStub(bin, name, `printf '%s\\n' "${name} $*" >> '${callLog}'\nexit ${exitCode}`);
    recordCalls("unshare");
    for (const tool of HOST_TOOLS) recordCalls(tool);
    recordCalls("apparmor_status", apparmorEnabled ? 0 : 1);
    const script = path.join(tmp, "after-install.sh");
    fs.writeFileSync(script, renderDebScript(DEB_AFTER_INSTALL));
    const result = spawnSync("/bin/bash", [script], {
      encoding: "utf8",
      env: { PATH: bin, LANG: "C" },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.doesNotMatch(result.stderr, /command not found/);
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

/** electron-builder 26.15.3's root-run namespace probe, which the fork replaces. */
const UPSTREAM_SANDBOX_SETUP = [
  "if ! { [[ -L /proc/self/ns/user ]] && unshare --user true; }; then",
  "    chmod 4755 '/opt/${sanitizedProductName}/chrome-sandbox' || true",
  "else",
  "    chmod 0755 '/opt/${sanitizedProductName}/chrome-sandbox' || true",
  "fi",
].join("\n");

/** Split a script's paragraphs, without comment lines, into chrome-sandbox setup and the rest. */
function splitSandboxSetup(script) {
  const paragraphs = script.split(/\n\s*\n/).map((paragraph) =>
    paragraph
      .split("\n")
      .filter((line) => !/^\s*#(?!!)/.test(line))
      .join("\n"),
  );
  return {
    sandboxSetup: paragraphs.filter((paragraph) => paragraph.includes("chrome-sandbox")),
    otherSteps: paragraphs.filter((paragraph) => !paragraph.includes("chrome-sandbox")),
  };
}

describe("Linux .deb packaging", { skip: process.platform !== "linux" }, () => {
  it("installs chrome-sandbox setuid root without probing user namespaces as root", () => {
    // Ubuntu with apparmor_restrict_unprivileged_userns=1: root's probe succeeds while the
    // desktop user's Chromium still cannot use the namespace sandbox.
    const calls = runPostInstall();
    assert.equal(chromeSandboxModes(calls).at(-1), "4755", `calls: ${JSON.stringify(calls)}`);
    assert.ok(!calls.some((call) => call.startsWith("unshare")), `calls: ${JSON.stringify(calls)}`);
  });

  it("keeps electron-builder's other post-install steps", () => {
    const calls = runPostInstall({ apparmorEnabled: true });
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

  const upstreamTemplate = upstreamAfterInstallTemplate();
  it(
    "matches electron-builder's template apart from the chrome-sandbox setup",
    { skip: !upstreamTemplate && "electron-builder is not installed" },
    () => {
      assert.ok(
        fs.existsSync(upstreamTemplate),
        `re-sync with electron-builder: ${upstreamTemplate} is gone`,
      );
      const fork = splitSandboxSetup(fs.readFileSync(DEB_AFTER_INSTALL, "utf8"));
      const upstream = splitSandboxSetup(fs.readFileSync(upstreamTemplate, "utf8"));
      const resync = `re-sync ${packageConfig.build.deb.afterInstall} with ${upstreamTemplate}`;
      assert.deepEqual(fork.otherSteps, upstream.otherSteps, resync);
      assert.deepEqual(upstream.sandboxSetup, [UPSTREAM_SANDBOX_SETUP], resync);
      assert.deepEqual(fork.sandboxSetup, [
        "chmod 4755 '/opt/${sanitizedProductName}/chrome-sandbox' || true",
      ]);
    },
  );
});
