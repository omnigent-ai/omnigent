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

import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

import { lastLine, SAFE_URL_RE } from "./arca.ts";
import {
  readArcaIdentity,
  IDENTITY_START,
  IDENTITY_END,
  type ArcaIdentity,
} from "./arcaIdentity.ts";
import { UV_INSTALLER } from "./cli_install.ts";
import type { SshConnectStatus } from "./ipc/bridge.ts";
import { createAskpass, type Askpass, type AskpassOptions } from "./ssh_askpass.ts";
import { isDatabricksOAuthServerUrl } from "./url.ts";

/** Includes time for the user to type a password or trust a new host. */
const OPEN_TIMEOUT_MS = 5 * 60 * 1000;
const SIGN_IN_TIMEOUT_MS = 10 * 60 * 1000;
const HOST_TIMEOUT_MS = 5 * 60 * 1000;
const INSTALL_TIMEOUT_MS = 10 * 60 * 1000;
const CONTROL_TIMEOUT_MS = 15_000;
const CLOSE_GRACE_MS = 2_000;
const MAX_OUTPUT_BYTES = 256 * 1024;

/** The Databricks CLI's sign-in listener range. */
export const SIGN_IN_FIRST_PORT = 8020;
export const SIGN_IN_LAST_PORT = 8040;

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
export class SshConnectError extends Error {}

/** A failed or canceled connect; `error` is fit to show the user. */
export interface SshConnectFailure {
  ok: false;
  error: string;
  canceled?: true;
}

export type SshConnectOutcome =
  { ok: true; alreadyRunning: boolean; identity?: ArcaIdentity } | SshConnectFailure;

/** `omnigent login`'s outcome; `missingCli` when the remote shell had no omnigent. */
type SignInResult =
  | { ok: true; missingCli?: undefined }
  | { ok: false; missingCli: true }
  | (SshConnectFailure & { missingCli?: undefined });

const canceledResult = (): SshConnectFailure => ({
  ok: false,
  canceled: true,
  error: "Connecting was canceled.",
});

/** A property of an untrusted value, read the way `value?.[key]` would. */
function propertyOf(value: unknown, key: string): unknown {
  return value == null ? undefined : (value as Record<string, unknown>)[key];
}

/** A child's exit code as Node reports it on `exit` and `close`. */
function exitCodeOf(value: unknown): number | null {
  // Node passes a number, or null after a signal; the cast keeps the value as is.
  return value as number | null;
}

/**
 * @returns The trimmed destination.
 * @throws {SshConnectError} When it isn't a plain ssh destination.
 */
export function validateDestination(raw: unknown): string {
  const destination = typeof raw === "string" ? raw.trim() : "";
  if (!DESTINATION_RE.test(destination)) {
    throw new SshConnectError(
      "Enter a host from your SSH config, a host name, or user@host. Spaces, quotes and a leading dash aren't allowed.",
    );
  }
  return destination;
}

/**
 * @returns The URL, safe to embed in a remote shell command.
 * @throws {SshConnectError}
 */
export function safeServerUrl(serverUrl: string): string {
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
 */
export function remoteCommand(script: string): string {
  if (/['\\]/.test(script)) throw new Error("remote script must not contain ' or \\");
  return `sh -c '${script}'`;
}

/** The installer puts omnigent in ~/.local/bin, which non-interactive shells often lack. */
const REMOTE_PATH = 'PATH="$HOME/.local/bin:$PATH"; export PATH';

/** @param serverUrl Already checked by {@link safeServerUrl}. */
export function loginScript(serverUrl: string): string {
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
 * @param serverUrl Already checked by {@link safeServerUrl}.
 */
export function hostScript(serverUrl: string): string {
  return [
    REMOTE_PATH,
    `omnigent host --server "${serverUrl}" --background --non-interactive && { echo; echo ${IDENTITY_START}; omnigent host status --server "${serverUrl}" --json; echo; echo ${IDENTITY_END}; }`,
  ].join("; ");
}

/** Install uv if needed, then run the app's bundled installer, piped on stdin. */
export function installScript(): string {
  return [
    'PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH',
    `if ! command -v uv >/dev/null 2>&1; then ${UV_INSTALLER} || exit 1; fi`,
    "sh -s -- --non-interactive",
  ].join("; ");
}

/** The commands shown in the consent window. */
export function describeCommands(serverUrl: string, destination: string): string[] {
  return [
    `ssh ${destination} omnigent login '${serverUrl}'`,
    `ssh ${destination} omnigent host --server '${serverUrl}' --background --non-interactive`,
  ];
}

/**
 * A sign-in link from `omnigent login` output. `port` is the loopback port the
 * CLI waits on, for a Databricks sign-in; null for a server's own browser
 * sign-in, which needs no forward.
 */
export interface SignInLink {
  url: string;
  port: number | null;
  kind: "databricks" | "server";
}

/** Find the sign-in link in one line of `omnigent login` output. */
export function parseSignInLink(line: string): SignInLink | null {
  const clean = line.replace(ANSI_RE, "").trim();
  const start = clean.indexOf("https://");
  if (start >= 0 && clean.includes("/oidc/v1/authorize")) {
    // split() always returns at least one element.
    const url = clean.slice(start).split(/\s/)[0]!;
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
  // The pattern's one group always matches.
  return own ? { url: own[1]!, port: null, kind: "server" } : null;
}

/**
 * Why not to open a sign-in link the remote machine printed, or null to open it.
 * The machine could be compromised, so its links are held to what the CLI prints:
 * the server's own page, or a workspace sign-in calling back to loopback on 8020 to 8040.
 */
export function signInLinkRefusal(link: SignInLink, serverUrl: string): string | null {
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

export type SshPromptKind = "confirm" | "secret" | "text";

/**
 * @param prompt The text ssh shows.
 * @param hint SSH_ASKPASS_PROMPT, "confirm" for yes/no questions.
 */
export function classifyPrompt(prompt: string, hint = ""): SshPromptKind {
  if (hint === "confirm" || /continue connecting|\(yes\/no/i.test(prompt)) return "confirm";
  if (/pass(word|phrase|code)|\bPIN\b|verification code|one-time|\btoken\b|\bOTP\b/i.test(prompt)) {
    return "secret";
  }
  return "text";
}

/**
 * @returns Whether nothing on this computer holds <port> on either loopback
 *   address; a browser may try `::1` first for localhost.
 */
export async function isPortFree(port: number): Promise<boolean> {
  return (await canListen(port, "127.0.0.1")) && (await canListen(port, "::1"));
}

/** Error codes that mean a computer has no IPv6 loopback, so nothing listens there. */
const NO_LOOPBACK_CODES: readonly (string | undefined)[] = ["EADDRNOTAVAIL", "EAFNOSUPPORT"];

function canListen(port: number, host: string): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", (error: NodeJS.ErrnoException | undefined) => {
      // A computer without IPv6 loopback has nothing listening there.
      resolve(host === "::1" && NO_LOOPBACK_CODES.includes(error?.code));
    });
    server.listen({ port, host, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}

/** Turn an ssh failure into one sentence for the user. */
export function describeSshFailure(
  code: number | null,
  stderr: string,
  destination: string,
): string {
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
 * @returns e.g. "Could not reach https://ws.example/v1/me: timed out", or "".
 */
function failureDetail(text: string): string {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.replace(ANSI_RE, "").trim())
    .filter(Boolean);
  const error = lines.findLast((line) => /^Error:\s*\S/.test(line));
  if (error) return error.replace(/^Error:\s*/, "").replace(/(https?:\/\/[^\s?#]*)[?#]\S*/g, "$1");
  return lines.findLast((line) => !/https?:\/\//.test(line)) ?? "";
}

/** Split a stream into lines, calling `onLine` for each complete one. */
function lineSplitter(onLine: (line: string) => void) {
  let pending = "";
  return {
    push(text: string) {
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

/** An ssh child's output stream; chunks are strings once the encoding is set. */
export interface SshChildOutput {
  setEncoding?: (encoding: "utf8") => unknown;
  on: (event: "data", listener: (chunk: unknown) => void) => unknown;
  destroy?: () => unknown;
}

export interface SshChildInput {
  on: (event: "error", listener: (error: unknown) => void) => unknown;
  end: (data: string) => unknown;
}

/** The part of a spawned ssh `ChildProcess` a session uses. */
export interface SshChild {
  stdin?: SshChildInput | null;
  stdout?: SshChildOutput | null;
  stderr?: SshChildOutput | null;
  kill: () => unknown;
  // Node passes an `Error` for "error", and the exit code for "exit" and "close".
  on: (event: "error" | "exit" | "close", listener: (value: unknown) => void) => unknown;
  once: (event: "exit" | "close", listener: () => void) => unknown;
}

export interface SshSpawnOptions {
  stdio: ["ignore" | "pipe", "ignore" | "pipe", "pipe"];
  env: NodeJS.ProcessEnv;
}

/** `child_process.spawn`, injectable so tests need no real ssh. */
export type SpawnSsh = (command: string, args: string[], options: SshSpawnOptions) => SshChild;

/** How one ssh client over the shared connection ended. */
export interface SshRunResult {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

/** A running remote command. */
export interface SshRun {
  promise: Promise<SshRunResult>;
  cancel: () => void;
}

/** `stdin` is written and closed; `holdStdin` keeps it open until the remote command ends. */
export interface SshExecOptions {
  stdin?: string | null | undefined;
  holdStdin?: boolean | undefined;
  timeoutMs?: number | undefined;
  onStdout?: ((text: string) => void) | undefined;
  onStderr?: ((text: string) => void) | undefined;
}

interface SshClientOptions extends SshExecOptions {
  baseArgs?: string[] | undefined;
}

/** `sshArgs` go before every call (tests pass `-F <config>`). */
export interface SshSessionOptions {
  destination: string;
  socketPath: string;
  env?: NodeJS.ProcessEnv | undefined;
  sshPath?: string | undefined;
  sshArgs?: string[] | undefined;
  spawn?: SpawnSsh | undefined;
  onOutput?: ((text: string) => void) | undefined;
  pathExists?: ((path: string) => boolean) | undefined;
  openTimeoutMs?: number | undefined;
}

/** What a connect does over the shared connection. */
export interface SshConnectSession {
  /** Connect, letting the askpass helper in `env` handle any prompt. */
  open: () => Promise<void>;
  /** Run a POSIX script on the remote machine. */
  exec: (script: string, opts?: SshExecOptions) => SshRun;
  forwardLocal: (port: number) => Promise<SshRunResult>;
  cancelLocal: (port: number) => Promise<SshRunResult>;
  holdRemote: (port: number) => Promise<SshRunResult>;
  releaseRemote: (port: number) => Promise<SshRunResult>;
  /** End the shared connection, killing it if it doesn't exit in time. */
  close: () => Promise<void>;
}

export interface SshSession extends SshConnectSession {
  destination: string;
  /** Kill every ssh process this session started, and start no more. */
  abort: () => void;
}

/** One shared ssh connection to `destination`. */
export function createSshSession(options: SshSessionOptions): SshSession {
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
  const spawnFn: SpawnSsh = options.spawn ?? spawn;
  const children = new Set<SshChild>();
  let master: SshChild | null = null;
  let masterExited = false;
  let aborted = false;

  /**
   * Spawn one ssh client over the shared connection. `stdin` is written and
   * closed; `holdStdin` keeps it open until the remote command ends.
   */
  function client(
    args: string[],
    {
      stdin = null,
      holdStdin = false,
      timeoutMs,
      onStdout,
      onStderr,
      baseArgs = sshArgs,
    }: SshClientOptions = {},
  ): SshRun {
    // Nothing new starts after abort or once the master exits: with the master gone,
    // ssh would quietly open a fresh connection, maybe to another machine, and carry on.
    if (aborted || masterExited) {
      const result = aborted
        ? { code: null, stdout: "", stderr: "", timedOut: false }
        : { code: 255, stdout: "", stderr: "the connection closed", timedOut: false };
      return { promise: Promise.resolve(result), cancel: () => {} };
    }
    let child: SshChild | undefined;
    let timedOut = false;
    let stdout = "";
    let stderr = "";
    const promise = new Promise<SshRunResult>((resolve) => {
      try {
        child = spawnFn(sshPath, [...baseArgs, "-S", socketPath, ...args], {
          stdio: [stdin === null && !holdStdin ? "ignore" : "pipe", "pipe", "pipe"],
          env,
        });
      } catch (error) {
        resolve({
          code: null,
          stdout: "",
          stderr: String(propertyOf(error, "message") ?? error),
          timedOut,
        });
        return;
      }
      const running = child;
      children.add(running);
      const timer = setTimeout(() => {
        timedOut = true;
        running.kill();
      }, timeoutMs ?? CONTROL_TIMEOUT_MS);
      if (typeof timer.unref === "function") timer.unref();
      running.stdout?.setEncoding?.("utf8");
      running.stderr?.setEncoding?.("utf8");
      // With the encoding set, chunks are strings; the casts keep the JS concatenation.
      running.stdout?.on("data", (chunk) => {
        stdout = (stdout + (chunk as string)).slice(-MAX_OUTPUT_BYTES);
        onStdout?.(String(chunk));
      });
      running.stderr?.on("data", (chunk) => {
        stderr = (stderr + (chunk as string)).slice(-MAX_OUTPUT_BYTES);
        onStderr?.(String(chunk));
      });
      running.stdin?.on("error", () => {});
      if (stdin !== null) running.stdin?.end(stdin);
      running.on("error", (error) => {
        stderr += String(propertyOf(error, "message") ?? error);
      });
      running.on("close", (code) => {
        clearTimeout(timer);
        children.delete(running);
        resolve({ code: exitCodeOf(code), stdout, stderr, timedOut });
      });
    });
    return { promise, cancel: () => child?.kill() };
  }

  // Control requests only talk to the master, so they skip the user's ssh
  // config: its forwards would ride along, and ClearAllForwardings there
  // would silently empty the ones asked for here.
  const control = (op: string, spec?: string[]) =>
    client(["-O", op, ...(spec ? spec : []), destination], { baseArgs: ["-F", "/dev/null"] })
      .promise;

  return {
    destination,

    /** Connect, letting the askpass helper in `env` handle any prompt. */
    open() {
      return new Promise<void>((resolve, reject) => {
        let stderr = "";
        let settled = false;
        let timer: NodeJS.Timeout | undefined;
        const finish = (error?: SshConnectError) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (error) reject(error);
          else resolve();
        };
        let started: SshChild;
        try {
          started = spawnFn(
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
          master = started;
        } catch {
          finish(new SshConnectError("Couldn't find ssh on this computer."));
          return;
        }
        children.add(started);
        started.stderr?.setEncoding?.("utf8");
        started.stderr?.on("data", (chunk) => {
          stderr = (stderr + (chunk as string)).slice(-MAX_OUTPUT_BYTES);
          onOutput(String(chunk));
        });
        started.on("error", (error) => {
          masterExited = true;
          finish(
            new SshConnectError(
              propertyOf(error, "code") === "ENOENT"
                ? "Couldn't find ssh on this computer."
                : `Couldn't run ssh: ${propertyOf(error, "message") ?? error}`,
            ),
          );
        });
        // `exit` comes first; `close` can wait on a ProxyCommand child holding stderr.
        started.on("exit", () => {
          masterExited = true;
        });
        // Callbacks read `master` as it is when they run; it is never cleared once set.
        started.on("close", (code) => {
          masterExited = true;
          children.delete(master!);
          finish(new SshConnectError(describeSshFailure(exitCodeOf(code), stderr, destination)));
        });
        timer = setTimeout(() => {
          master!.kill();
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

    /** Run a POSIX script on the remote machine. */
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
        await new Promise<void>((resolve) => {
          // Checked above; `master` is never cleared once set.
          const timer = setTimeout(() => {
            master!.kill();
            resolve();
          }, CLOSE_GRACE_MS);
          const done = () => {
            clearTimeout(timer);
            resolve();
          };
          master!.once("exit", done);
          master!.once("close", done);
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

/** Per-step limits, for tests; each defaults to the module's own. */
export interface SshConnectTimeouts {
  signIn?: number;
  host?: number;
  install?: number;
}

/** Everything one connect needs; see {@link runSshConnect}. */
export interface SshConnectContext {
  serverUrl: string;
  destination: string;
  session: SshConnectSession;
  openExternal: (url: string) => Promise<void> | void;
  onStatus: (status: SshConnectStatus) => void;
  onOutput: (text: string) => void;
  askInstall: () => Promise<boolean>;
  readInstallScript: () => string | null;
  isPortFree?: (port: number) => Promise<boolean>;
  isCanceled?: () => boolean;
  onIdentityUnavailable?: ((reason: string) => void) | undefined;
  timeouts?: SshConnectTimeouts;
}

type SignInContext = SshConnectContext &
  Required<Pick<SshConnectContext, "isPortFree" | "isCanceled">>;

/** Run `omnigent login` on the remote machine, forwarding its sign-in port. */
async function signIn(ctx: SignInContext): Promise<SignInResult> {
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
  // One result per busy port, in order.
  const held = busy.filter((_, index) => holds[index]!.code === 0);
  // Take the port the CLI should pick before it starts: tools that forward new
  // remote listeners to this computer (VS Code, for example) would grab it first.
  let forwarded =
    expected !== null && (await session.forwardLocal(expected)).code === 0 ? expected : null;
  if (ctx.isCanceled()) return canceledResult();

  onStatus({ step: "signing-in" });
  const notes: string[] = [];
  let opened = false;
  let problem: string | null = null;
  let queue = Promise.resolve();
  // Assigned by session.exec below, before any output line is handled.
  let run!: SshRun;
  const handleLine = async (line: string) => {
    if (problem || ctx.isCanceled()) return;
    // The OIDC code and any error come after the link, so only the link stops counting.
    const link = opened ? null : parseSignInLink(line);
    if (!link) {
      const clean = line.replace(ANSI_RE, "").trim();
      const code = /^Confirm this code in the browser:\s*(\S+)/.exec(clean);
      // The pattern's one group always matches.
      if (code) onStatus({ step: "confirm-code", code: code[1]! });
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
  const enqueue = (line: string) => {
    queue = queue
      .then(() => handleLine(line))
      .catch((error: unknown) => {
        problem ??=
          error instanceof SshConnectError
            ? error.message
            : `Sign-in failed: ${propertyOf(error, "message") ?? error}`;
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
 */
export async function runSshConnect(ctx: SshConnectContext): Promise<SshConnectOutcome> {
  const full = { isPortFree, isCanceled: () => false, ...ctx };
  const { session, serverUrl, destination, onStatus, onOutput } = full;
  const openSafely = async (url: string) => {
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

/** A prompt for the user, from ssh through the askpass helper. */
export interface SshPromptRequest {
  kind: SshPromptKind;
  text: string;
  signal: AbortSignal;
}

/** Where a connect shows its progress and asks its questions. */
export interface SshConnectHooks {
  onOutput: (text: string) => void;
  onStatus: (status: SshConnectStatus) => void;
  /** Resolves with the answer, or null when the user cancels. */
  prompt: (request: SshPromptRequest) => Promise<string | null>;
  askInstall: () => Promise<boolean>;
}

export interface StartSshConnectDeps {
  hooks: SshConnectHooks;
  openExternal: (url: string) => Promise<void> | void;
  readInstallScript: () => string | null;
  log?: ((message: string) => void) | undefined;
  makeTempDir?: (() => string) | undefined;
  createAskpass?: ((options: AskpassOptions) => Promise<Askpass>) | undefined;
  createSession?:
    ((options: SshSessionOptions) => SshConnectSession & Pick<SshSession, "abort">) | undefined;
  isPortFree?: ((port: number) => Promise<boolean>) | undefined;
}

/** A connect in flight; `cancel` kills its ssh processes. */
export interface SshConnectRun {
  promise: Promise<SshConnectOutcome>;
  cancel: () => void;
}

/** Connect `destination` to `serverUrl`, with prompts routed through `hooks`. */
export function startSshConnect(
  serverUrl: string,
  destination: string,
  deps: StartSshConnectDeps,
): SshConnectRun {
  const { hooks, log = () => {} } = deps;
  const makeTempDir =
    deps.makeTempDir ?? (() => fs.mkdtempSync(path.join(os.tmpdir(), "omni-ssh-")));
  let canceled = false;
  let session: (SshConnectSession & Pick<SshSession, "abort">) | null = null;
  const promise = (async (): Promise<SshConnectOutcome> => {
    let dir: string | null = null;
    let askpass: Askpass | null = null;
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
      log(`ssh connect: unexpected failure: ${propertyOf(error, "stack") ?? error}`);
      return {
        ok: false,
        error: `Connecting to ${destination} failed: ${propertyOf(error, "message") ?? error}`,
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
