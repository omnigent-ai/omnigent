"use strict";

/**
 * Route ssh's prompts (password, key passphrase, 2FA code, new host key) to
 * the desktop app instead of a terminal.
 *
 * With SSH_ASKPASS_REQUIRE=force (OpenSSH 8.4+), ssh runs the program named in
 * SSH_ASKPASS for every prompt and reads the answer from its stdout. That
 * program is a shell script in a private temp directory. It leaves the question
 * there as a file and reads the answer from a named pipe, so the app's own
 * binary is never started as a script runner. Answers are never written to disk.
 */

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const MAX_REQUEST_BYTES = 64 * 1024;
/** With its two framing bytes an answer fits one 4096-byte pipe write. */
const MAX_ANSWER_BYTES = 4094;
/** What the script reads when there is no answer: the user cancelled, or the app is closing. */
const NO_ANSWER_LINE = "-\n";
const SCAN_INTERVAL_MS = 100;

/**
 * What ssh runs. Its process id names the question file (`q.<pid>`: token, hint,
 * then the prompt) and the answer pipe (`r.<pid>`: `+<answer>`, or `-` for no
 * answer). The rename makes the question appear whole. The script holds both
 * ends of its pipe, so a line written at any time is kept until it reads it;
 * the watcher writes the no-answer line when the app is gone.
 */
const ASKPASS_SCRIPT = `#!/bin/sh
dir=$OMNIGENT_SSH_ASKPASS_DIR
app=$OMNIGENT_SSH_ASKPASS_APP_PID
ask=$dir/q.$$
reply=$dir/r.$$
watch=
umask 077
trap 'rm -f "$ask.tmp" "$ask" "$reply"; [ -z "$watch" ] || kill "$watch" 2>/dev/null' EXIT
mkfifo "$reply" || exit 1
exec 3<>"$reply" || exit 1
printf '%s\\n%s\\n%s' "$OMNIGENT_SSH_ASKPASS_TOKEN" "$SSH_ASKPASS_PROMPT" "$1" >"$ask.tmp" || exit 1
mv "$ask.tmp" "$ask" || exit 1
(
  while kill -0 "$app" 2>/dev/null && kill -0 $$ 2>/dev/null; do sleep 1; done
  printf '%s\\n' - >&3
) </dev/null >/dev/null 2>&1 &
watch=$!
answer=
IFS= read -r answer <&3
case $answer in
  +*) printf '%s\\n' "\${answer#+}" ;;
  *) exit 1 ;;
esac
`;

/**
 * @param {unknown} given
 * @param {string} expected
 * @returns {boolean}
 */
function tokensMatch(given, expected) {
  if (typeof given !== "string") return false;
  // Compare encoded bytes: timingSafeEqual throws on buffers of different lengths.
  const givenBytes = Buffer.from(given);
  const expectedBytes = Buffer.from(expected);
  return (
    givenBytes.length === expectedBytes.length && crypto.timingSafeEqual(givenBytes, expectedBytes)
  );
}

/**
 * @param {number} pid
 * @returns {boolean} whether that process still exists
 */
function isRunning(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM: it exists, but belongs to someone else.
    return error?.code === "EPERM";
  }
}

/**
 * Write the script into `dir` and start answering its questions.
 *
 * @param {{
 *   dir: string,
 *   onPrompt: (request: { prompt: string, hint: string, signal: AbortSignal }) => Promise<string | null>,
 * }} options `dir` must be private to this user (mode 0700). `onPrompt`
 *   resolves with the answer, or null when the user cancels; `signal` aborts
 *   when ssh stops waiting for the answer.
 * @returns {Promise<{ env: Record<string, string>, close: () => void }>} The
 *   environment to give ssh, and a function that stops answering.
 */
async function createAskpass({ dir, onPrompt }) {
  const token = crypto.randomBytes(24).toString("hex");
  const scriptPath = path.join(dir, "askpass");
  fs.writeFileSync(scriptPath, ASKPASS_SCRIPT, { mode: 0o700 });

  /**
   * Questions not yet answered, by the script's process id. `answer` is
   * undefined until the user decides, then the answer or null for a cancel.
   *
   * @type {Map<string, { asked: AbortController, answer: string | null | undefined }>}
   */
  const waiting = new Map();
  let closed = false;

  /** Write one line into a script's answer pipe; a script that has left is not an error. */
  const reply = (pipePath, line) => {
    let pipe = null;
    try {
      // Non-blocking: the open fails at once when no script holds the pipe.
      pipe = fs.openSync(pipePath, fs.constants.O_WRONLY | fs.constants.O_NONBLOCK);
      const bytes = Buffer.from(line);
      let written = 0;
      // A full pipe reports "try again"; the script is reading, so a few tries drain it.
      for (let attempt = 0; written < bytes.length && attempt < 100; attempt += 1) {
        try {
          written += fs.writeSync(pipe, bytes, written);
        } catch (error) {
          if (error?.code !== "EAGAIN") throw error;
        }
      }
    } catch {
      // The script left before reading its answer.
    } finally {
      if (pipe !== null) fs.closeSync(pipe);
    }
  };

  /** Move one question along: answer it once the user has decided. */
  const advance = (id, entry) => {
    if (!isRunning(Number(id))) {
      // Nothing waits for this answer any more: the script was killed, say with its ssh.
      waiting.delete(id);
      entry.asked.abort();
      return;
    }
    if (entry.answer === undefined) return;
    waiting.delete(id);
    reply(path.join(dir, `r.${id}`), entry.answer === null ? NO_ANSWER_LINE : `+${entry.answer}\n`);
  };

  const accept = (id) => {
    const entry = { asked: new AbortController(), answer: undefined };
    waiting.set(id, entry);
    const settle = (answer) => {
      if (waiting.get(id) !== entry) return;
      const usable = typeof answer === "string" && Buffer.byteLength(answer) <= MAX_ANSWER_BYTES;
      // ssh reads one line, so nothing after a line break would reach it.
      entry.answer = usable ? answer.split(/\r?\n/, 1)[0] : null;
      advance(id, entry);
    };
    const askPath = path.join(dir, `q.${id}`);
    let request = null;
    try {
      if (fs.statSync(askPath).size <= MAX_REQUEST_BYTES) {
        request = fs.readFileSync(askPath, "utf8");
      }
      fs.unlinkSync(askPath);
    } catch {
      request = null;
    }
    const [givenToken, hint, ...promptLines] = (request ?? "").split("\n");
    if (request === null || !tokensMatch(givenToken, token)) {
      settle(null);
      return;
    }
    Promise.resolve()
      .then(() => onPrompt({ prompt: promptLines.join("\n"), hint, signal: entry.asked.signal }))
      .catch(() => null)
      .then(settle);
  };

  const scan = () => {
    let names;
    try {
      names = fs.readdirSync(dir);
    } catch {
      return;
    }
    for (const name of names) {
      const id = /^q\.(\d+)$/.exec(name)?.[1];
      if (id && !waiting.has(id)) accept(id);
    }
    for (const [id, entry] of waiting) advance(id, entry);
  };
  // Scanned, not watched: prompts are rare, and a scan needs no file events.
  const timer = setInterval(scan, SCAN_INTERVAL_MS);

  const close = () => {
    if (closed) return;
    closed = true;
    clearInterval(timer);
    for (const entry of waiting.values()) entry.asked.abort();
    waiting.clear();
    let names = [];
    try {
      names = fs.readdirSync(dir);
    } catch {
      // The directory is already gone, and the scripts' pipes with it.
    }
    for (const name of names) {
      if (!/^r\.\d+$/.test(name)) continue;
      // Release every script still waiting, asked or not; one that died left only the name.
      const pipePath = path.join(dir, name);
      reply(pipePath, NO_ANSWER_LINE);
      fs.rmSync(pipePath, { force: true });
    }
  };

  return {
    env: {
      SSH_ASKPASS: scriptPath,
      SSH_ASKPASS_REQUIRE: "force",
      OMNIGENT_SSH_ASKPASS_DIR: dir,
      OMNIGENT_SSH_ASKPASS_APP_PID: String(process.pid),
      OMNIGENT_SSH_ASKPASS_TOKEN: token,
    },
    close,
  };
}

module.exports = { createAskpass };
