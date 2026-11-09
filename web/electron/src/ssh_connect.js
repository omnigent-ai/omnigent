"use strict";

/**
 * Connect a machine the user reaches over SSH as an Omnigent host.
 *
 * `databricks auth login` on the remote machine waits for the browser's
 * redirect on its own `localhost:<port>`, which a laptop browser can't reach.
 * This module opens one shared OpenSSH connection (ControlMaster) with the
 * user's own `ssh` and config, runs `omnigent login` over it, forwards exactly
 * the sign-in port the CLI picked, opens the sign-in link in the laptop's
 * browser, then starts `omnigent host --background` and closes the connection.
 * The host keeps its own outbound tunnel to the server afterwards.
 *
 * The CLI takes 8020, or the next free port up to 8040, and can't be told
 * which. So before sign-in, ports that are busy on the laptop are taken on the
 * remote machine with `ssh -R`, and the CLI lands on one free on both.
 *
 * Spawns and port probes are injected so everything is unit-testable without
 * a real ssh.
 */

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { lastLine, SAFE_URL_RE } = require("./arca");
const { isDatabricksOAuthServerUrl } = require("./url");
const { readArcaIdentity, IDENTITY_START, IDENTITY_END } = require("./arcaIdentity");
const { UV_INSTALLER } = require("./cli_install");
const { createAskpass } = require("./ssh_askpass");

/** Includes time for the user to type a password or trust a new host. */
const OPEN_TIMEOUT_MS = 5 * 60 * 1000;
const SIGN_IN_TIMEOUT_MS = 10 * 60 * 1000;
const HOST_TIMEOUT_MS = 5 * 60 * 1000;
const INSTALL_TIMEOUT_MS = 10 * 60 * 1000;
const CONTROL_TIMEOUT_MS = 15_000;
const CLOSE_GRACE_MS = 2_000;
const MAX_OUTPUT_BYTES = 256 * 1024;

/** The Databricks CLI's sign-in listener range. */
const SIGN_IN_FIRST_PORT = 8020;
const SIGN_IN_LAST_PORT = 8040;

/** Target for ports held on the remote machine; nothing ever connects to them. */
const HELD_PORT_TARGET = "127.0.0.1:9";

/**
 * An ssh destination as typed or listed in ~/.ssh/config: an alias, a host, or
 * user@host. A leading `-` would read as an ssh option, so it is refused.
 */
const DESTINATION_RE = /^[A-Za-z0-9_][A-Za-z0-9._@-]{0,252}$/;

// eslint-disable-next-line no-control-regex -- intentionally stripping terminal escape codes
const ANSI_RE = /\u001b\[[0-9;?]*[A-Za-z]/g;

/**
 * Error with a message fit to show the user as-is.
 */
class SshConnectError extends Error {}

const canceledResult = () => ({ ok: false, canceled: true, error: "Connecting was canceled." });

/**
 * @param {unknown} raw
 * @returns {string} The trimmed destination.
 * @throws {SshConnectError} When it isn't a plain ssh destination.
 */
function validateDestination(raw) {
  const destination = typeof raw === "string" ? raw.trim() : "";
  if (!DESTINATION_RE.test(destination)) {
    throw new SshConnectError(
      "Enter a host from your SSH config, a host name, or user@host. Spaces, quotes and a leading dash aren't allowed.",
    );
  }
  return destination;
}

/**
 * @param {string} serverUrl
 * @returns {string} The URL, safe to embed in a remote shell command.
 * @throws {SshConnectError}
 */
function safeServerUrl(serverUrl) {
  let url;
  try {
    url = new URL(serverUrl);
  } catch {
    throw new SshConnectError("This window's server URL isn't valid.");
  }
  if (!["https:", "http:"].includes(url.protocol) || !SAFE_URL_RE.test(url.toString())) {
    throw new SshConnectError(
      "This window's server URL has characters that can't be sent in an SSH command.",
    );
  }
  return url.toString();
}

/**
 * Wrap a POSIX script so any login shell (bash, zsh, fish) passes it to sh
 * unchanged. The script must not contain single quotes or backslashes.
 *
 * @param {string} script
 * @returns {string}
 */
function remoteCommand(script) {
  if (/['\\]/.test(script)) throw new Error("remote script must not contain ' or \\");
  return `sh -c '${script}'`;
}

/** The installer puts omnigent in ~/.local/bin, which non-interactive shells often lack. */
const REMOTE_PATH = 'PATH="$HOME/.local/bin:$PATH"; export PATH';

/**
 * @param {string} serverUrl Already checked by {@link safeServerUrl}.
 * @returns {string}
 */
function loginScript(serverUrl) {
  // The app holds stdin open; when the session ends it closes, and the
  // watchdog stops the login and its CLI instead of leaving them on the port.
  // setsid puts both in one process group to kill; pkill covers hosts without it (macOS).
  return [
    REMOTE_PATH,
    "exec 3<&0",
    "group=; command -v setsid >/dev/null 2>&1 && group=setsid",
    `BROWSER=none $group omnigent login "${serverUrl}" </dev/null & login=$!`,
    "(read -r _ <&3; kill -s TERM -- -$login 2>/dev/null || { command -v pkill >/dev/null 2>&1 && pkill -TERM -P $login; kill -TERM $login 2>/dev/null; }) & watch=$!",
    "wait $login; status=$?; kill $watch 2>/dev/null; exit $status",
  ].join("; ");
}

/**
 * Start the host, then print its status between markers for {@link readArcaIdentity}.
 *
 * @param {string} serverUrl Already checked by {@link safeServerUrl}.
 * @returns {string}
 */
function hostScript(serverUrl) {
  return [
    REMOTE_PATH,
    `omnigent host --server "${serverUrl}" --background --non-interactive && { echo; echo ${IDENTITY_START}; omnigent host status --server "${serverUrl}" --json; echo; echo ${IDENTITY_END}; }`,
  ].join("; ");
}

/** Install uv if needed, then run the app's bundled installer, piped on stdin. */
function installScript() {
  return [
    'PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH',
    `if ! command -v uv >/dev/null 2>&1; then ${UV_INSTALLER} || exit 1; fi`,
    "sh -s -- --non-interactive",
  ].join("; ");
}

/**
 * The commands shown in the consent window.
 *
 * @param {string} serverUrl
 * @param {string} destination
 * @returns {string[]}
 */
function describeCommands(serverUrl, destination) {
  return [
    `ssh ${destination} omnigent login '${serverUrl}'`,
    `ssh ${destination} omnigent host --server '${serverUrl}' --background --non-interactive`,
  ];
}

/**
 * Find the sign-in link in one line of `omnigent login` output.
 *
 * @param {string} line
 * @returns {{ url: string, port: number | null, kind: "databricks" | "server" } | null}
 *   `port` is the loopback port the CLI waits on, for a Databricks sign-in; null
 *   for a server's own browser sign-in, which needs no forward.
 */
function parseSignInLink(line) {
  const clean = line.replace(ANSI_RE, "").trim();
  const start = clean.indexOf("https://");
  if (start >= 0 && clean.includes("/oidc/v1/authorize")) {
    const url = clean.slice(start).split(/\s/)[0];
    try {
      const redirect = new URL(new URL(url).searchParams.get("redirect_uri") ?? "");
      const port = Number(redirect.port);
      if (["localhost", "127.0.0.1"].includes(redirect.hostname) && port > 0) {
        return { url, port, kind: "databricks" };
      }
    } catch {
      // No usable redirect_uri; signInLinkRefusal stops it.
    }
    return { url, port: null, kind: "databricks" };
  }
  const own = /^Opening browser for login:\s+(https?:\/\/\S+)/.exec(clean);
  return own ? { url: own[1], port: null, kind: "server" } : null;
}

/**
 * Why not to open a sign-in link the remote machine printed, or null to open it.
 * The machine could be compromised, so its links are held to what the CLI prints:
 * the server's own page, or a workspace sign-in calling back to loopback on 8020 to 8040.
 *
 * @param {{ url: string, port: number | null, kind: "databricks" | "server" }} link
 * @param {string} serverUrl
 * @returns {string | null}
 */
function signInLinkRefusal(link, serverUrl) {
  let origin;
  try {
    origin = new URL(link.url).origin;
  } catch {
    return "The sign-in link isn't a web address.";
  }
  const serverOrigin = new URL(serverUrl).origin;
  if (link.kind === "server" && origin !== serverOrigin) {
    return `The sign-in link goes to ${origin}, not this server (${serverOrigin}).`;
  }
  if (link.kind === "server") return null;
  // Workspace and account sign-in hosts only: Databricks Apps hosts serve app content.
  if (origin !== serverOrigin && !isDatabricksOAuthServerUrl(link.url)) {
    return `The sign-in link goes to ${origin}, which isn't this server or a Databricks workspace.`;
  }
  if (link.port === null) {
    return "The sign-in link doesn't call back to this computer, so sign-in can't finish here.";
  }
  if (link.port < SIGN_IN_FIRST_PORT || link.port > SIGN_IN_LAST_PORT) {
    return `The sign-in link waits on port ${link.port}, outside ${SIGN_IN_FIRST_PORT} to ${SIGN_IN_LAST_PORT}.`;
  }
  return null;
}

/**
 * @param {string} prompt The text ssh shows.
 * @param {string} [hint] SSH_ASKPASS_PROMPT, "confirm" for yes/no questions.
 * @returns {"confirm" | "secret" | "text"}
 */
function classifyPrompt(prompt, hint = "") {
  if (hint === "confirm" || /continue connecting|\(yes\/no/i.test(prompt)) return "confirm";
  if (/pass(word|phrase|code)|\bPIN\b|verification code|one-time|\btoken\b|\bOTP\b/i.test(prompt)) {
    return "secret";
  }
  return "text";
}

/**
 * @param {number} port
 * @returns {Promise<boolean>} Whether nothing on this computer holds <port> on
 *   either loopback address; a browser may try `::1` first for localhost.
 */
async function isPortFree(port) {
  return (await canListen(port, "127.0.0.1")) && (await canListen(port, "::1"));
}

function canListen(port, host) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", (error) => {
      // A computer without IPv6 loopback has nothing listening there.
      resolve(host === "::1" && ["EADDRNOTAVAIL", "EAFNOSUPPORT"].includes(error?.code));
    });
    server.listen({ port, host, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}

/**
 * Turn an ssh failure into one sentence for the user.
 *
 * @param {number | null} code
 * @param {string} stderr
 * @param {string} destination
 * @returns {string}
 */
function describeSshFailure(code, stderr, destination) {
  if (/REMOTE HOST IDENTIFICATION HAS CHANGED/i.test(stderr)) {
    return `${destination}'s host key changed since you last connected. Check with whoever manages it before trusting it again.`;
  }
  if (/Host key verification failed/i.test(stderr)) {
    return `${destination} wasn't trusted, so the connection stopped.`;
  }
  if (/Permission denied/i.test(stderr)) {
    return `${destination} didn't accept your sign-in. Check your SSH key or password, then try again.`;
  }
  if (/Could not resolve hostname/i.test(stderr)) {
    return `Couldn't find a machine called ${destination}. Check the name or your ~/.ssh/config.`;
  }
  if (/Connection refused|timed out|No route to host|Network is unreachable/i.test(stderr)) {
    return `Couldn't reach ${destination}: ${lastLine(stderr.trim())}`;
  }
  const detail = stderr.trim();
  return detail
    ? `Couldn't connect to ${destination}: ${lastLine(detail)}`
    : `Couldn't connect to ${destination} (exit code ${code ?? "unknown"}).`;
}

/**
 * Pick the line of remote output that says what went wrong: the CLI's own
 * `Error: ...` line, which generic hints may follow, with link queries cut
 * (sign-in links carry one-time values). Else the last line without a link.
 *
 * @param {string} text
 * @returns {string} e.g. "Could not reach https://ws.example/v1/me: timed out", or "".
 */
function failureDetail(text) {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.replace(ANSI_RE, "").trim())
    .filter(Boolean);
  const error = lines.findLast((line) => /^Error:\s*\S/.test(line));
  if (error) return error.replace(/^Error:\s*/, "").replace(/(https?:\/\/[^\s?#]*)[?#]\S*/g, "$1");
  return lines.findLast((line) => !/https?:\/\//.test(line)) ?? "";
}

/**
 * Split a stream into lines, calling `onLine` for each complete one.
 *
 * @param {(line: string) => void} onLine
 */
function lineSplitter(onLine) {
  let pending = "";
  return {
    push(text) {
      pending += text;
      let newline = pending.indexOf("\n");
      while (newline >= 0) {
        onLine(pending.slice(0, newline).replace(/\r$/, ""));
        pending = pending.slice(newline + 1);
        newline = pending.indexOf("\n");
      }
    },
    flush() {
      if (pending) onLine(pending);
      pending = "";
    },
  };
}

/**
 * One shared ssh connection to `destination`.
 *
 * @param {{
 *   destination: string,
 *   socketPath: string,
 *   env?: NodeJS.ProcessEnv,
 *   sshPath?: string,
 *   sshArgs?: string[],
 *   spawn?: typeof spawn,
 *   onOutput?: (text: string) => void,
 *   pathExists?: (p: string) => boolean,
 *   openTimeoutMs?: number,
 * }} options `sshArgs` go before every call (tests pass `-F <config>`).
 */
function createSshSession(options) {
  const {
    destination,
    socketPath,
    env = process.env,
    sshPath = "ssh",
    sshArgs = [],
    onOutput = () => {},
    pathExists = fs.existsSync,
    openTimeoutMs = OPEN_TIMEOUT_MS,
  } = options;
  const spawnFn = options.spawn ?? spawn;
  const children = new Set();
  let master = null;
  let masterExited = false;
  let aborted = false;

  /**
   * Spawn one ssh client over the shared connection. `stdin` is written and
   * closed; `holdStdin` keeps it open until the remote command ends.
   */
  function client(
    args,
    { stdin = null, holdStdin = false, timeoutMs, onStdout, onStderr, baseArgs = sshArgs } = {},
  ) {
    // Nothing new starts after abort or once the master exits: with the master gone,
    // ssh would quietly open a fresh connection, maybe to another machine, and carry on.
    if (aborted || masterExited) {
      const result = aborted
        ? { code: null, stdout: "", stderr: "", timedOut: false }
        : { code: 255, stdout: "", stderr: "the connection closed", timedOut: false };
      return { promise: Promise.resolve(result), cancel: () => {} };
    }
    let child;
    let timedOut = false;
    let stdout = "";
    let stderr = "";
    const promise = new Promise((resolve) => {
      try {
        child = spawnFn(sshPath, [...baseArgs, "-S", socketPath, ...args], {
          stdio: [stdin === null && !holdStdin ? "ignore" : "pipe", "pipe", "pipe"],
          env,
        });
      } catch (error) {
        resolve({ code: null, stdout: "", stderr: String(error?.message ?? error), timedOut });
        return;
      }
      children.add(child);
      const timer = setTimeout(() => {
        timedOut = true;
        child.kill();
      }, timeoutMs ?? CONTROL_TIMEOUT_MS);
      if (typeof timer.unref === "function") timer.unref();
      child.stdout?.setEncoding?.("utf8");
      child.stderr?.setEncoding?.("utf8");
      child.stdout?.on("data", (chunk) => {
        stdout = (stdout + chunk).slice(-MAX_OUTPUT_BYTES);
        onStdout?.(String(chunk));
      });
      child.stderr?.on("data", (chunk) => {
        stderr = (stderr + chunk).slice(-MAX_OUTPUT_BYTES);
        onStderr?.(String(chunk));
      });
      child.stdin?.on("error", () => {});
      if (stdin !== null) child.stdin?.end(stdin);
      child.on("error", (error) => {
        stderr += String(error?.message ?? error);
      });
      child.on("close", (code) => {
        clearTimeout(timer);
        children.delete(child);
        resolve({ code, stdout, stderr, timedOut });
      });
    });
    return { promise, cancel: () => child?.kill() };
  }

  // Control requests only talk to the master, so they skip the user's ssh
  // config: its forwards would ride along, and ClearAllForwardings there
  // would silently empty the ones asked for here.
  const control = (op, spec) =>
    client(["-O", op, ...(spec ? spec : []), destination], { baseArgs: ["-F", "/dev/null"] })
      .promise;

  return {
    destination,

    /** Connect, letting the askpass helper in `env` handle any prompt. */
    open() {
      return new Promise((resolve, reject) => {
        let stderr = "";
        let settled = false;
        let timer = null;
        const finish = (error) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (error) reject(error);
          else resolve();
        };
        try {
          master = spawnFn(
            sshPath,
            [
              ...sshArgs,
              "-M",
              "-S",
              socketPath,
              "-o",
              "ControlPersist=no",
              // Forwards from the user's config belong to their own sessions
              // and can clash with them; this connection makes its own.
              "-o",
              "ClearAllForwardings=yes",
              "-o",
              "ServerAliveInterval=30",
              "-o",
              "ConnectTimeout=20",
              "-N",
              destination,
            ],
            { stdio: ["ignore", "ignore", "pipe"], env },
          );
        } catch {
          finish(new SshConnectError("Couldn't find ssh on this computer."));
          return;
        }
        children.add(master);
        master.stderr?.setEncoding?.("utf8");
        master.stderr?.on("data", (chunk) => {
          stderr = (stderr + chunk).slice(-MAX_OUTPUT_BYTES);
          onOutput(String(chunk));
        });
        master.on("error", (error) => {
          masterExited = true;
          finish(
            new SshConnectError(
              error?.code === "ENOENT"
                ? "Couldn't find ssh on this computer."
                : `Couldn't run ssh: ${error?.message ?? error}`,
            ),
          );
        });
        // `exit` comes first; `close` can wait on a ProxyCommand child holding stderr.
        master.on("exit", () => {
          masterExited = true;
        });
        master.on("close", (code) => {
          masterExited = true;
          children.delete(master);
          finish(new SshConnectError(describeSshFailure(code, stderr, destination)));
        });
        timer = setTimeout(() => {
          master.kill();
          finish(new SshConnectError(`Connecting to ${destination} timed out.`));
        }, openTimeoutMs);
        if (typeof timer.unref === "function") timer.unref();
        // The control socket appears once authentication succeeds.
        const poll = async () => {
          if (settled || masterExited) return;
          if (pathExists(socketPath) && (await control("check")).code === 0) {
            finish();
            return;
          }
          setTimeout(poll, 200);
        };
        void poll();
      });
    },

    /**
     * Run a POSIX script on the remote machine.
     *
     * @param {string} script
     * @param {{ stdin?: string | null, holdStdin?: boolean, timeoutMs?: number, onStdout?: (t: string) => void, onStderr?: (t: string) => void }} [opts]
     */
    exec(script, opts = {}) {
      // Use the master even when the config says ControlMaster yes or ask, which skip
      // it; don't re-request the config's forwards; and run the command even when the
      // alias sets RemoteCommand (say, tmux), which ssh would otherwise refuse.
      return client(
        [
          "-o",
          "ControlMaster=no",
          "-o",
          "ClearAllForwardings=yes",
          "-o",
          "RemoteCommand=none",
          "-T",
          destination,
          remoteCommand(script),
        ],
        opts,
      );
    },

    // An explicit localhost bind keeps the listener local even when the
    // user's config sets GatewayPorts, which the master reads.
    forwardLocal: (port) => control("forward", ["-L", `localhost:${port}:127.0.0.1:${port}`]),
    cancelLocal: (port) => control("cancel", ["-L", `localhost:${port}:127.0.0.1:${port}`]),
    holdRemote: (port) => control("forward", ["-R", `${port}:${HELD_PORT_TARGET}`]),
    releaseRemote: (port) => control("cancel", ["-R", `${port}:${HELD_PORT_TARGET}`]),

    /** End the shared connection, killing it if it doesn't exit in time. */
    async close() {
      if (!master) return;
      if (!masterExited) await control("exit");
      if (!masterExited) {
        await new Promise((resolve) => {
          const timer = setTimeout(() => {
            master.kill();
            resolve();
          }, CLOSE_GRACE_MS);
          const done = () => {
            clearTimeout(timer);
            resolve();
          };
          master.once("exit", done);
          master.once("close", done);
        });
      }
      // A ProxyCommand child can outlive ssh while holding its stderr open.
      master.stderr?.destroy?.();
    },

    /** Kill every ssh process this session started, and start no more. */
    abort() {
      aborted = true;
      for (const child of children) {
        try {
          child.kill();
        } catch {
          // Already gone.
        }
      }
    },
  };
}

/**
 * Run `omnigent login` on the remote machine, forwarding its sign-in port.
 *
 * @returns {Promise<{ ok: true } | { ok: false, missingCli?: true, error?: string }>}
 */
async function signIn(ctx) {
  const { session, serverUrl, destination, openExternal, onStatus, isPortFree: portFree } = ctx;
  const ports = Array.from(
    { length: SIGN_IN_LAST_PORT - SIGN_IN_FIRST_PORT + 1 },
    (_, index) => SIGN_IN_FIRST_PORT + index,
  );
  const free = await Promise.all(ports.map((port) => portFree(port)));
  // The first port free here is the one the CLI should land on. With none free, only a
  // server's own sign-in (no callback port) can work, so hold nothing and let it run.
  const expected = ports.find((_, index) => free[index]) ?? null;
  const busy = expected === null ? [] : ports.filter((_, index) => !free[index]);
  // A port the remote machine already uses can't be held, but the CLI skips it anyway.
  const holds = await Promise.all(busy.map((port) => session.holdRemote(port)));
  const held = busy.filter((_, index) => holds[index].code === 0);
  // Take the port the CLI should pick before it starts: tools that forward new
  // remote listeners to this computer (Arca Companion, VS Code) would grab it first.
  let forwarded =
    expected !== null && (await session.forwardLocal(expected)).code === 0 ? expected : null;
  if (ctx.isCanceled()) return canceledResult();

  onStatus({ step: "signing-in" });
  const notes = [];
  let opened = false;
  let problem = null;
  let queue = Promise.resolve();
  let run = null;
  const handleLine = async (line) => {
    if (problem || ctx.isCanceled()) return;
    // The OIDC code and any error come after the link, so only the link stops counting.
    const link = opened ? null : parseSignInLink(line);
    if (!link) {
      const clean = line.replace(ANSI_RE, "").trim();
      const code = /^Confirm this code in the browser:\s*(\S+)/.exec(clean);
      if (code) onStatus({ step: "confirm-code", code: code[1] });
      // Kept only to explain a failure; failureDetail drops one-time link values.
      if (clean) notes.push(clean);
      return;
    }
    const refusal = signInLinkRefusal(link, serverUrl);
    if (refusal) {
      problem = refusal;
      run.cancel();
      return;
    }
    if (link.port !== null && link.port !== forwarded) {
      if (expected === null) {
        problem = `No free sign-in port on this computer: ${SIGN_IN_FIRST_PORT} to ${SIGN_IN_LAST_PORT} are all in use.`;
        run.cancel();
        return;
      }
      // The remote machine had the expected port in use, so the CLI moved on.
      if (forwarded !== null) {
        await session.cancelLocal(forwarded);
        forwarded = null;
      }
      if (!(await portFree(link.port))) {
        problem = `Port ${link.port} became busy on this computer during sign-in. Try again.`;
        run.cancel();
        return;
      }
      const forward = await session.forwardLocal(link.port);
      if (forward.code !== 0) {
        problem = `Couldn't forward port ${link.port} from ${destination}: ${lastLine(forward.stderr.trim() || "ssh refused the forward")}`;
        run.cancel();
        return;
      }
      forwarded = link.port;
    }
    opened = true;
    await openExternal(link.url);
    onStatus({ step: "waiting-for-browser" });
  };
  const enqueue = (line) => {
    queue = queue
      .then(() => handleLine(line))
      .catch((error) => {
        problem ??=
          error instanceof SshConnectError
            ? error.message
            : `Sign-in failed: ${error?.message ?? error}`;
        run.cancel();
      });
  };
  const stdoutLines = lineSplitter(enqueue);
  const stderrLines = lineSplitter(enqueue);
  run = session.exec(loginScript(serverUrl), {
    holdStdin: true,
    timeoutMs: ctx.timeouts?.signIn ?? SIGN_IN_TIMEOUT_MS,
    onStdout: (text) => stdoutLines.push(text),
    onStderr: (text) => stderrLines.push(text),
  });
  const result = await run.promise;
  stdoutLines.flush();
  stderrLines.flush();
  await queue;
  if (forwarded !== null) await session.cancelLocal(forwarded);
  await Promise.all(held.map((port) => session.releaseRemote(port)));

  if (problem) return { ok: false, error: problem };
  if (ctx.isCanceled()) return canceledResult();
  if (result.timedOut)
    return { ok: false, error: "Sign-in timed out. Try again to get a new link." };
  if (result.code === 0) return { ok: true };
  if (result.code === 127) return { ok: false, missingCli: true };
  if (result.code === 255) {
    return { ok: false, error: describeSshFailure(result.code, result.stderr, destination) };
  }
  if (notes.some((note) => /^Username\b/.test(note)) && notes.some((note) => /Abort/.test(note))) {
    return {
      ok: false,
      error: `This server signs in with a username and password. Run \`omnigent login ${serverUrl}\` on ${destination} in a terminal, then connect again.`,
    };
  }
  const detail = failureDetail(notes.join("\n"));
  return {
    ok: false,
    error: detail
      ? `Signing in on ${destination} failed: ${detail}`
      : `Signing in on ${destination} failed (exit code ${result.code ?? "unknown"}).`,
  };
}

/**
 * The whole connect: sign in (installing omnigent first if the user agrees),
 * then start the host.
 *
 * @param {{
 *   serverUrl: string,
 *   destination: string,
 *   session: ReturnType<typeof createSshSession>,
 *   openExternal: (url: string) => Promise<void> | void,
 *   onStatus: (status: { step: string, code?: string }) => void,
 *   onOutput: (text: string) => void,
 *   askInstall: () => Promise<boolean>,
 *   readInstallScript: () => string | null,
 *   isPortFree?: (port: number) => Promise<boolean>,
 *   isCanceled?: () => boolean,
 *   onIdentityUnavailable?: (reason: string) => void,
 *   timeouts?: { signIn?: number, host?: number, install?: number },
 * }} ctx
 * @returns {Promise<{ ok: boolean, error?: string, canceled?: boolean, alreadyRunning?: boolean, identity?: { serverUrl: string, hostId: string } }>}
 */
async function runSshConnect(ctx) {
  const full = { isPortFree, isCanceled: () => false, ...ctx };
  const { session, serverUrl, destination, onStatus, onOutput } = full;
  const openSafely = async (url) => {
    const parsed = new URL(url);
    if (!["https:", "http:"].includes(parsed.protocol)) {
      throw new SshConnectError("The sign-in link isn't a web address.");
    }
    await full.openExternal(parsed.toString());
  };
  onStatus({ step: "connecting" });
  await session.open();
  try {
    let login = await signIn({ ...full, openExternal: openSafely });
    if (!login.ok && login.missingCli) {
      if (!(await full.askInstall())) {
        return { ok: false, canceled: true, error: `Omnigent isn't installed on ${destination}.` };
      }
      if (full.isCanceled()) return canceledResult();
      const installer = full.readInstallScript();
      if (!installer)
        return { ok: false, error: "This build doesn't include the Omnigent installer." };
      onStatus({ step: "installing" });
      const install = await session.exec(installScript(), {
        stdin: installer,
        timeoutMs: full.timeouts?.install ?? INSTALL_TIMEOUT_MS,
        onStdout: onOutput,
        onStderr: onOutput,
      }).promise;
      if (install.code !== 0) {
        const detail = failureDetail(install.stderr || install.stdout);
        return {
          ok: false,
          error: install.timedOut
            ? `Installing Omnigent on ${destination} timed out.`
            : `Installing Omnigent on ${destination} failed: ${detail || `exit code ${install.code}`}`,
        };
      }
      login = await signIn({ ...full, openExternal: openSafely });
    }
    if (!login.ok) {
      return login.missingCli
        ? { ok: false, error: `Omnigent still isn't on the PATH on ${destination}.` }
        : login;
    }

    if (full.isCanceled()) return canceledResult();
    onStatus({ step: "starting-host" });
    const host = await session.exec(hostScript(serverUrl), {
      timeoutMs: full.timeouts?.host ?? HOST_TIMEOUT_MS,
      onStdout: onOutput,
      onStderr: onOutput,
    }).promise;
    if (full.isCanceled()) return canceledResult();
    if (host.code !== 0) {
      const detail = failureDetail(host.stderr || host.stdout);
      return {
        ok: false,
        error: host.timedOut
          ? `Starting the Omnigent host on ${destination} timed out.`
          : `Starting the Omnigent host on ${destination} failed: ${detail || `exit code ${host.code}`}`,
      };
    }
    const identity = readArcaIdentity(host.stdout, serverUrl, full.onIdentityUnavailable);
    return {
      ok: true,
      alreadyRunning: /already running/i.test(host.stdout + host.stderr),
      ...(identity ? { identity } : {}),
    };
  } finally {
    await session.close();
  }
}

/**
 * Connect `destination` to `serverUrl`, with prompts routed through `hooks`.
 *
 * @param {string} serverUrl
 * @param {string} destination
 * @param {{
 *   hooks: {
 *     onOutput: (text: string) => void,
 *     onStatus: (status: { step: string, code?: string }) => void,
 *     prompt: (request: { kind: string, text: string }) => Promise<string | null>,
 *     askInstall: () => Promise<boolean>,
 *   },
 *   openExternal: (url: string) => Promise<void> | void,
 *   readInstallScript: () => string | null,
 *   log?: (message: string) => void,
 *   makeTempDir?: () => string,
 *   createAskpass?: typeof createAskpass,
 *   createSession?: typeof createSshSession,
 *   isPortFree?: (port: number) => Promise<boolean>,
 * }} deps
 * @returns {{ promise: Promise<object>, cancel: () => void }}
 */
function startSshConnect(serverUrl, destination, deps) {
  const { hooks, log = () => {} } = deps;
  const makeTempDir =
    deps.makeTempDir ?? (() => fs.mkdtempSync(path.join(os.tmpdir(), "omni-ssh-")));
  let canceled = false;
  let session = null;
  const promise = (async () => {
    let dir = null;
    let askpass = null;
    try {
      const url = safeServerUrl(serverUrl);
      const target = validateDestination(destination);
      dir = makeTempDir();
      fs.chmodSync(dir, 0o700);
      askpass = await (deps.createAskpass ?? createAskpass)({
        dir,
        onPrompt: ({ prompt, hint, signal }) =>
          hooks.prompt({ kind: classifyPrompt(prompt, hint), text: prompt, signal }),
      });
      session = (deps.createSession ?? createSshSession)({
        destination: target,
        socketPath: path.join(dir, "m"),
        env: { ...process.env, ...askpass.env },
        onOutput: hooks.onOutput,
      });
      if (canceled) return canceledResult();
      return await runSshConnect({
        serverUrl: url,
        destination: target,
        session,
        openExternal: deps.openExternal,
        onStatus: hooks.onStatus,
        onOutput: hooks.onOutput,
        askInstall: hooks.askInstall,
        readInstallScript: deps.readInstallScript,
        isPortFree: deps.isPortFree ?? isPortFree,
        isCanceled: () => canceled,
        onIdentityUnavailable: (reason) => log(`ssh connect: host identity unavailable: ${reason}`),
      });
    } catch (error) {
      if (canceled) return canceledResult();
      if (error instanceof SshConnectError) return { ok: false, error: error.message };
      log(`ssh connect: unexpected failure: ${error?.stack ?? error}`);
      return {
        ok: false,
        error: `Connecting to ${destination} failed: ${error?.message ?? error}`,
      };
    } finally {
      askpass?.close();
      if (dir) fs.rmSync(dir, { recursive: true, force: true });
    }
  })();
  return {
    promise,
    cancel: () => {
      canceled = true;
      session?.abort();
    },
  };
}

module.exports = {
  SIGN_IN_FIRST_PORT,
  SIGN_IN_LAST_PORT,
  SshConnectError,
  classifyPrompt,
  createSshSession,
  describeCommands,
  describeSshFailure,
  hostScript,
  installScript,
  isPortFree,
  loginScript,
  parseSignInLink,
  signInLinkRefusal,
  remoteCommand,
  runSshConnect,
  safeServerUrl,
  startSshConnect,
  validateDestination,
};
