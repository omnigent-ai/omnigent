// Sends the server page's workspace API calls to a host's local socket, which a
// page can't open itself. Only API calls, and only to sockets an Omnigent host
// made for this user, so a page can't reach other local services.

"use strict";

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

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
 * @param {{socketPath: string, path: string, headers: Record<string, string>, body: Uint8Array}} request
 * @returns {Promise<{status: number, headers: Record<string, string>, body: Uint8Array}>}
 */
function workspaceSocketRequest(request) {
  const { socketPath, path: methodPath, headers, body } = request ?? {};
  if (!isWorkspaceSocket(socketPath)) {
    return Promise.reject(new Error("not a workspace socket on this machine"));
  }
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
