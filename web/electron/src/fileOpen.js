"use strict";

const { execFile } = require("node:child_process");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { promisify } = require("node:util");
const { discoverArcaSshHost } = require("./arcaSshDiscovery");
const { readMacAppIcon } = require("./macAppIcon");

const MAC_APPS = [
  { id: "vscode", label: "VS Code", bundle: "com.microsoft.VSCode", name: "Visual Studio Code" },
  { id: "cursor", label: "Cursor", bundle: "com.todesktop.230313mzl4w4u92", name: "Cursor" },
  { id: "emacs", label: "Emacs", bundle: "org.gnu.Emacs", name: "Emacs" },
  {
    id: "terminal",
    label: "Terminal",
    bundle: "com.apple.Terminal",
    name: "Terminal",
    directory: true,
  },
  { id: "iterm", label: "iTerm2", bundle: "com.googlecode.iterm2", name: "iTerm", directory: true },
  { id: "xcode", label: "Xcode", bundle: "com.apple.dt.Xcode", name: "Xcode" },
  {
    id: "android-studio",
    label: "Android Studio",
    bundle: "com.google.android.studio",
    name: "Android Studio",
  },
  { id: "idea", label: "IntelliJ IDEA", bundle: "com.jetbrains.intellij", name: "IntelliJ IDEA" },
];
const DEFAULT_APP = { id: "default", label: "Default app" };
const DISCOVERY_TTL_MS = 30_000;

/** Open files with a fixed app choice; never accept a command from the renderer. */
function registerFileOpen({
  ipcMain,
  shell,
  isPinnedOriginSender,
  localHostId,
  platform = process.platform,
  run = promisify(execFile),
  getAppIcon = readMacAppIcon,
  confirmDefaultOpen = async () => false,
  getServerContext = () => null,
  now = Date.now,
  resolveSshHost = () => null,
  discoverSshHost = discoverArcaSshHost,
  appDirectories = [
    "/Applications",
    path.join(os.homedir(), "Applications"),
    "/System/Applications/Utilities",
  ],
}) {
  const iconCache = new Map();
  const appCache = new Map();
  const sshContexts = new WeakMap();
  function sshContext(event) {
    const context = getServerContext(event);
    if (!context || !event.sender) return null;
    let state = sshContexts.get(event.sender);
    if (!state || state.context !== context) {
      state = { context, hosts: new Map() };
      sshContexts.set(event.sender, state);
    }
    return state;
  }

  function findApp(app) {
    const cached = appCache.get(app.bundle);
    if (cached && now() < cached.expiresAt) return cached.promise;
    const promise = locateApp(app).catch(() => null);
    appCache.set(app.bundle, { promise, expiresAt: now() + DISCOVERY_TTL_MS });
    return promise;
  }

  async function locateApp({ name, bundle }) {
    const candidates = await Promise.all(
      appDirectories.map(async (directory) => {
        const candidate = path.join(directory, `${name}.app`);
        return (await fs.stat(candidate).catch(() => null))?.isDirectory() ? candidate : null;
      }),
    );
    const installed = candidates.find(Boolean);
    if (installed) return installed;
    // Spotlight also finds renamed apps and custom installation locations.
    const { stdout } = await run(
      "/usr/bin/mdfind",
      ["-0", `kMDItemCFBundleIdentifier == "${bundle}"`],
      {
        timeout: 3000,
      },
    );
    return stdout.split("\0").find((candidate) => path.isAbsolute(candidate)) ?? null;
  }

  ipcMain.handle("omnigent:file-open-apps", async (event) => {
    if (!isPinnedOriginSender(event)) return [];
    if (platform !== "darwin") return [DEFAULT_APP];
    const installed = await Promise.all(
      MAC_APPS.map(async (app) => {
        const { id, label, directory } = app;
        try {
          const appPath = await findApp(app);
          if (!appPath) return null;
          if (!iconCache.has(appPath))
            iconCache.set(
              appPath,
              getAppIcon(appPath).catch(() => null),
            );
          const icon = await iconCache.get(appPath);
          return {
            id,
            label,
            ...(icon ? { icon } : {}),
            ...(directory ? { directory: true } : {}),
          };
        } catch {
          return null;
        }
      }),
    );
    return [...installed.filter(Boolean), DEFAULT_APP];
  });

  ipcMain.handle("omnigent:open-file-in-app", async (event, hostId, rawPath, appId) => {
    if (!isPinnedOriginSender(event)) return false;
    if (typeof hostId !== "string" || !hostId || hostId !== localHostId()) return false;
    if (typeof rawPath !== "string" || rawPath.includes("\0") || !path.isAbsolute(rawPath)) {
      return false;
    }
    const selectedApp = platform === "darwin" && MAC_APPS.find((app) => app.id === appId);
    if (appId !== "default" && !selectedApp) return false;
    try {
      const filePath = path.normalize(rawPath);
      const stat = await fs.stat(filePath);
      if (appId === "default") {
        const context = getServerContext(event);
        if (!context || !(await confirmDefaultOpen(event, filePath))) return false;
        if (!isPinnedOriginSender(event) || getServerContext(event) !== context) return false;
        return (await shell.openPath(filePath)) === "";
      }
      const target =
        selectedApp.directory && !stat.isDirectory() ? path.dirname(filePath) : filePath;
      await run("/usr/bin/open", ["-b", selectedApp.bundle, target], { timeout: 5000 });
      return true;
    } catch {
      return false;
    }
  });

  ipcMain.handle("omnigent:file-open-ssh-host", async (event, hostId, fingerprints) => {
    if (!isPinnedOriginSender(event) || typeof hostId !== "string" || !hostId) return null;
    const state = sshContext(event);
    if (!state || platform !== "darwin" || hostId === localHostId()) return null;
    try {
      const verifiedAlias = await resolveSshHost(event, hostId);
      if (!isPinnedOriginSender(event) || sshContext(event) !== state) return null;
      if (verifiedAlias) {
        state.hosts.set(hostId, { alias: verifiedAlias, verified: true });
        return verifiedAlias;
      }
      const previous = state.hosts.get(hostId);
      const keys = fingerprints ?? previous?.fingerprints;
      const fingerprintKey = JSON.stringify(keys);
      if (
        previous &&
        !previous.verified &&
        previous.fingerprintKey === fingerprintKey &&
        now() < previous.expiresAt
      )
        return previous.promise;
      const entry = {
        alias: null,
        fingerprints: keys,
        fingerprintKey,
        expiresAt: now() + DISCOVERY_TTL_MS,
      };
      state.hosts.set(hostId, entry);
      entry.promise = Promise.resolve()
        .then(() => discoverSshHost(keys))
        .catch(() => null)
        .then((alias) => {
          if (
            !isPinnedOriginSender(event) ||
            sshContext(event) !== state ||
            state.hosts.get(hostId) !== entry
          )
            return null;
          entry.alias = alias;
          return alias;
        });
      return entry.promise;
    } catch {
      state.hosts.delete(hostId);
      return null;
    }
  });

  ipcMain.handle(
    "omnigent:open-remote-file-in-app",
    async (event, hostId, remotePath, appId, sshHost, directory) => {
      if (!isPinnedOriginSender(event) || platform !== "darwin") return false;
      if (typeof hostId !== "string" || !hostId || hostId === localHostId()) return false;
      if (typeof sshHost !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(sshHost))
        return false;
      if (
        typeof remotePath !== "string" ||
        !remotePath.startsWith("/") ||
        remotePath.includes("\0")
      ) {
        return false;
      }
      if (typeof directory !== "boolean" || !["cursor", "vscode"].includes(appId)) return false;
      try {
        const state = sshContext(event);
        const entry = state?.hosts.get(hostId);
        if (!entry?.alias || entry.alias !== sshHost) return false;
        if (entry.verified && (await resolveSshHost(event, hostId)) !== entry.alias) return false;
        const stillAuthorized = () =>
          isPinnedOriginSender(event) &&
          sshContext(event) === state &&
          state.hosts.get(hostId) === entry;
        const selectedApp = MAC_APPS.find((app) => app.id === appId);
        const appPath = await findApp(selectedApp);
        if (!appPath || !stillAuthorized()) return false;
        const executable = path.join(
          appPath,
          "Contents",
          "Resources",
          "app",
          "bin",
          appId === "cursor" ? "cursor" : "code",
        );
        const env = { ...process.env, CURSOR_CLI_BLOCK_CURSOR_AGENT: "true" };
        // A shell-launched desktop must not reuse an inherited editor's IPC connection.
        delete env.VSCODE_IPC_HOOK_CLI;
        // Foreground the app before its CLI initializes and hands off the SSH workspace.
        await run("/usr/bin/open", ["-a", appPath], { timeout: 5000 }).catch(() => {});
        if (!stillAuthorized()) return false;
        await run(
          executable,
          [
            "--remote",
            `ssh-remote+${entry.alias}`,
            ...(directory ? [] : ["--goto"]),
            "--",
            directory ? remotePath : `${remotePath}:1:1`,
          ],
          { timeout: 30000, env },
        );
        return true;
      } catch {
        return false;
      }
    },
  );
}

module.exports = { registerFileOpen };
