/**
 * Route ssh's prompts (password, key passphrase, 2FA code, new host key) to
 * the desktop app instead of a terminal.
 *
 * With SSH_ASKPASS_REQUIRE=force (OpenSSH 8.4+), ssh runs the program named in
 * SSH_ASKPASS for every prompt and reads the answer from its stdout. That
 * program is a two-line script, written to a private temp directory, which runs
 * this app's own binary in Node mode and asks the main process over a Unix
 * socket guarded by a one-time token. Answers are never written to disk.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";

const MAX_REQUEST_BYTES = 64 * 1024;

/** Runs under ELECTRON_RUN_AS_NODE; prints the answer, or exits 1 on cancel. */
const HELPER_SOURCE = `"use strict";
const net = require("node:net");
const socket = net.createConnection(process.env.OMNIGENT_SSH_ASKPASS_SOCKET);
let reply = "";
socket.setEncoding("utf8");
socket.on("connect", () => {
  socket.write(
    JSON.stringify({
      token: process.env.OMNIGENT_SSH_ASKPASS_TOKEN,
      prompt: process.argv[2] || "",
      hint: process.env.SSH_ASKPASS_PROMPT || "",
    }) + "\\n",
  );
});
socket.on("data", (chunk) => {
  reply += chunk;
});
socket.on("end", () => {
  let answer = null;
  try {
    answer = JSON.parse(reply).answer;
  } catch {
    answer = null;
  }
  if (typeof answer !== "string") process.exit(1);
  process.stdout.write(answer + "\\n");
});
socket.on("error", () => process.exit(1));
`;

/** Single-quote `value` for /bin/sh. */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

function tokensMatch(given: unknown, expected: string): boolean {
  if (typeof given !== "string") return false;
  // Compare encoded bytes: timingSafeEqual throws on buffers of different lengths.
  const givenBytes = Buffer.from(given);
  const expectedBytes = Buffer.from(expected);
  return (
    givenBytes.length === expectedBytes.length && crypto.timingSafeEqual(givenBytes, expectedBytes)
  );
}

/** One prompt from ssh; `signal` aborts when ssh stops waiting for the answer. */
export interface AskpassRequest {
  prompt: string;
  hint: string;
  signal: AbortSignal;
}

export interface AskpassOptions {
  /** Must be private to this user (mode 0700). */
  dir: string;
  /** Resolves with the answer, or null when the user cancels. */
  onPrompt: (request: AskpassRequest) => Promise<string | null>;
  execPath?: string;
}

/** The environment to give ssh, and a function that stops the socket. */
export interface Askpass {
  env: Record<string, string>;
  close: () => void;
}

/** Start the prompt socket and write the helper into `dir`. */
export async function createAskpass({
  dir,
  onPrompt,
  execPath = process.execPath,
}: AskpassOptions): Promise<Askpass> {
  const token = crypto.randomBytes(24).toString("hex");
  const socketPath = path.join(dir, "a");
  const helperPath = path.join(dir, "askpass.js");
  const wrapperPath = path.join(dir, "askpass");
  fs.writeFileSync(helperPath, HELPER_SOURCE, { mode: 0o600 });
  fs.writeFileSync(
    wrapperPath,
    `#!/bin/sh\nELECTRON_RUN_AS_NODE=1 exec ${shellQuote(execPath)} ${shellQuote(helperPath)} "$@"\n`,
    { mode: 0o700 },
  );

  const server = net.createServer((socket) => {
    let request = "";
    // The helper exits when ssh gives up (say, a timeout), so its question is moot.
    const asked = new AbortController();
    socket.on("close", () => asked.abort());
    socket.setEncoding("utf8");
    socket.on("error", () => {});
    const onData = (chunk: string) => {
      request += chunk;
      const newline = request.indexOf("\n");
      if (newline < 0) {
        if (request.length > MAX_REQUEST_BYTES) socket.destroy();
        return;
      }
      socket.off("data", onData);
      let parsed: unknown;
      try {
        parsed = JSON.parse(request.slice(0, newline));
      } catch {
        socket.destroy();
        return;
      }
      // The request is untrusted JSON; read it the way `parsed?.[key]` would.
      const fields = (parsed ?? {}) as Record<string, unknown>;
      if (!tokensMatch(fields.token, token)) {
        socket.destroy();
        return;
      }
      Promise.resolve()
        .then(() =>
          onPrompt({
            prompt: String(fields.prompt ?? ""),
            hint: String(fields.hint ?? ""),
            signal: asked.signal,
          }),
        )
        .catch(() => null)
        .then((answer) => {
          socket.end(`${JSON.stringify(typeof answer === "string" ? { answer } : {})}\n`);
        });
    };
    socket.on("data", onData);
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, () => {
      server.off("error", reject);
      resolve();
    });
  });
  fs.chmodSync(socketPath, 0o600);
  return {
    env: {
      SSH_ASKPASS: wrapperPath,
      SSH_ASKPASS_REQUIRE: "force",
      OMNIGENT_SSH_ASKPASS_SOCKET: socketPath,
      OMNIGENT_SSH_ASKPASS_TOKEN: token,
    },
    close: () => server.close(),
  };
}
