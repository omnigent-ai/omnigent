"use strict";

const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const { canonicalHostId } = require("./sshHosts");
const { REMOTE_PATH, remoteCommand, safeServerUrl, validateDestination } = require("./ssh_connect");
const { normalizeSavedServerUrl } = require("./url");

function targetUrl(raw) {
  const url = new URL(normalizeSavedServerUrl(raw));
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new Error("invalid server target");
  }
  url.hash = "";
  url.pathname = url.pathname.replace(/\/+$/, "") || "/";
  url.searchParams.sort();
  return url;
}

/** Read an existing daemon's identity over the user's SSH, without starting or signing in. */
async function verifySshPreviewIdentity(serverUrl, hostId, destination, run = promisify(execFile)) {
  try {
    const server = safeServerUrl(serverUrl);
    const target = validateDestination(destination);
    const expected = targetUrl(server);
    const { stdout } = await run(
      "ssh",
      [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "RemoteCommand=none",
        "-T",
        target,
        remoteCommand(
          `${REMOTE_PATH}; OMNIGENT_WRAPPER_BYPASS=1 omnigent host status --server "${server}" --json`,
        ),
      ],
      { timeout: 15_000, maxBuffer: 64 * 1024, encoding: "utf8" },
    );
    // CLI wrappers may print a selected-target notice before the JSON object.
    const { daemons } = JSON.parse(stdout.slice(stdout.indexOf("{")));
    if (!Array.isArray(daemons) || daemons.length !== 1) return false;
    const daemon = daemons[0];
    if (
      daemon.mode !== "server" ||
      daemon.process !== "online" ||
      daemon.host_status !== "online" ||
      daemon.error != null ||
      typeof daemon.host_id !== "string" ||
      !/^[a-f0-9]{32}$/i.test(daemon.host_id) ||
      canonicalHostId(daemon.host_id) !== canonicalHostId(hostId)
    )
      return false;
    // The selected workspace must match; the CLI's server_url omits its query.
    if (targetUrl(daemon.target).href !== expected.href) return false;
    expected.search = "";
    return targetUrl(daemon.server_url).href === expected.href;
  } catch {
    return false;
  }
}

module.exports = { verifySshPreviewIdentity };
