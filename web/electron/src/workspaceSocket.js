// Sends the server page's workspace API calls to a host's local socket, which a
// page can't open itself. Only API calls, and only to sockets an Omnigent host
// made for this user, so a page can't reach other local services.

"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { suggestedAlias } = require("./sshTargets");

// The host binds <temp dir>/omnigent-workspace-XXXX/workspace.sock, in a 0700 directory.
const SOCKET_NAME = "workspace.sock";
const DIRECTORY_PREFIX = "omnigent-workspace-";
const METHOD_PATH = /^\/omnigent\.api\.workspace\.v1alpha1\.WorkspaceService\/[A-Za-z]+$/;
const FORWARDED_HEADERS = [
  "authorization",
  "content-type",
  "connect-protocol-version",
  "connect-timeout-ms",
];
const MAX_REQUEST_BYTES = 1024 * 1024;
// The host caps a read at 10 MiB; this leaves room for framing.
const MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
const TIMEOUT_MS = 30_000;
// A host's socket on another machine, which can't be checked from here. Only
// characters `ssh -L` can carry, in the directory shape the host makes.
const REMOTE_SOCKET_PATH =
  /^(\/[A-Za-z0-9._-]+)*\/omnigent-workspace-[A-Za-z0-9_]+\/workspace\.sock$/;
// A unary call the host answers only for a valid token, so a 200 proves the right host.
const PROBE_PATH = "/omnigent.api.workspace.v1alpha1.WorkspaceService/ListDirectory";
const PROBE_BODY = Buffer.from('{"pageSize":1}');
// The host's answer to a refused token. Main gives it to the page instead of sending the
// call to an unproven host, and the page renews its token.
const REFUSED_BODY = Buffer.from(
  '{"code":"unauthenticated","message":"the host refused the token"}',
);

/**
 * Whether `socketPath` is a workspace socket an Omnigent host made for this user.
 *
 * @param {unknown} socketPath
 * @returns {boolean}
 */
function isWorkspaceSocket(socketPath) {
  if (typeof socketPath !== "string" || typeof process.getuid !== "function") return false;
  if (!path.isAbsolute(socketPath) || path.normalize(socketPath) !== socketPath) return false;
  const directory = path.dirname(socketPath);
  if (path.basename(socketPath) !== SOCKET_NAME) return false;
  if (!path.basename(directory).startsWith(DIRECTORY_PREFIX)) return false;
  const uid = process.getuid();
  try {
    const directoryStat = fs.lstatSync(directory);
    const socketStat = fs.lstatSync(socketPath);
    return (
      directoryStat.isDirectory() &&
      directoryStat.uid === uid &&
      (directoryStat.mode & 0o077) === 0 &&
      socketStat.isSocket() &&
      socketStat.uid === uid
    );
  } catch {
    // Missing, e.g. the session's host runs on another machine.
    return false;
  }
}

/**
 * Send one workspace API call to a host's socket and buffer its response.
 *
 * The socket is this machine's, or one on the host's machine reached over SSH
 * through `forwards`: by the destination recorded for the host, or else by the
 * alias the page suggests once the user allows it and `verifySshTarget` proves
 * it reaches the host.
 *
 * @param {{hostId?: string, sshTarget?: string, socketPath: string, path: string, headers: Record<string, string>, body: Uint8Array}} request
 * @param {{
 *   sshDestination?: (hostId: string) => string | null,
 *   verifySshTarget?: (hostId: string, alias: string, proves: () => Promise<boolean | "refused">) => Promise<boolean | "refused">,
 *   forwards?: ReturnType<typeof import("./workspaceSshForwards").createWorkspaceSshForwards>,
 * }} [routes]
 * @returns {Promise<{status: number, headers: Record<string, string>, body: Uint8Array}>}
 */
function workspaceSocketRequest(request, routes = {}) {
  const { hostId, sshTarget, socketPath, path: methodPath, headers, body } = request ?? {};
  if (typeof methodPath !== "string" || !METHOD_PATH.test(methodPath)) {
    return Promise.reject(new Error("not a workspace API method"));
  }
  if (!ArrayBuffer.isView(body) || body.byteLength > MAX_REQUEST_BYTES) {
    return Promise.reject(new Error("invalid request body"));
  }
  const forwarded = { "content-length": String(body.byteLength) };
  for (const name of FORWARDED_HEADERS) {
    const value = headers?.[name];
    if (typeof value === "string") forwarded[name] = value;
  }
  if (isWorkspaceSocket(socketPath)) return send(socketPath, methodPath, forwarded, body);
  const recorded = typeof hostId === "string" ? (routes.sshDestination?.(hostId) ?? null) : null;
  const suggested =
    !recorded && typeof hostId === "string" && routes.verifySshTarget
      ? suggestedAlias(sshTarget)
      : null;
  const destination = recorded ?? suggested;
  const { forwards } = routes;
  const remotePathOk =
    typeof socketPath === "string" &&
    REMOTE_SOCKET_PATH.test(socketPath) &&
    path.posix.normalize(socketPath) === socketPath;
  if (!destination || !forwards || !remotePathOk) {
    return Promise.reject(new Error("not a workspace socket on this machine"));
  }
  const proves = async () => {
    const { socketPath: local, dropForward } = await forwards.localSocket(destination, socketPath);
    const probe = {
      "content-type": "application/json",
      "content-length": String(PROBE_BODY.byteLength),
      ...(forwarded.authorization ? { authorization: forwarded.authorization } : {}),
    };
    let proof = false;
    try {
      const { status } = await send(local, PROBE_PATH, probe, PROBE_BODY);
      if (status === 200) proof = true;
      else if (status === 401) proof = "refused";
    } finally {
      // The connection may serve other hosts; it closes once idle, or now if it is gone.
      if (proof !== true) dropForward();
    }
    return proof;
  };
  const forward = recorded
    ? forwards.localSocket(destination, socketPath)
    : routes.verifySshTarget(hostId, destination, proves).then((verdict) => {
        if (verdict === "refused") return null;
        if (!verdict) throw new Error("SSH alias not allowed or not verified for this host");
        return forwards.localSocket(destination, socketPath);
      });
  return forward.then((reached) => {
    if (!reached) {
      const refusal = new Uint8Array(REFUSED_BODY);
      return { status: 401, headers: { "content-type": "application/json" }, body: refusal };
    }
    const { socketPath: local, dropConnection, dropForward } = reached;
    return send(local, methodPath, forwarded, body).catch((error) => {
      // A missing listener means the connection is gone. A reset means the remote socket
      // refused (a restarted host) or the master died; cancelling the forward tells which.
      if (error?.code === "ECONNREFUSED" || error?.code === "ENOENT") dropConnection();
      else if (error?.code === "ECONNRESET") dropForward();
      throw error;
    });
  });
}

function send(socketPath, methodPath, forwarded, body) {
  return new Promise((resolve, reject) => {
    const outgoing = http.request(
      { socketPath, path: methodPath, method: "POST", headers: forwarded, timeout: TIMEOUT_MS },
      (response) => {
        const chunks = [];
        let size = 0;
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > MAX_RESPONSE_BYTES) {
            // Reject first: once the response has started, destroy() reports a generic error.
            reject(new Error("workspace API response too large"));
            outgoing.destroy();
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          const responseHeaders = {};
          for (const [name, value] of Object.entries(response.headers)) {
            if (typeof value === "string") responseHeaders[name] = value;
          }
          resolve({
            status: response.statusCode ?? 0,
            headers: responseHeaders,
            body: new Uint8Array(Buffer.concat(chunks)),
          });
        });
        response.on("error", reject);
      },
    );
    outgoing.on("timeout", () => outgoing.destroy(new Error("workspace API call timed out")));
    outgoing.on("error", reject);
    outgoing.end(Buffer.from(body.buffer, body.byteOffset, body.byteLength));
  });
}

module.exports = { isWorkspaceSocket, workspaceSocketRequest };
