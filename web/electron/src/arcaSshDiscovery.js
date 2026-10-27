"use strict";

const { execFile } = require("node:child_process");
const { createHash } = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { promisify } = require("node:util");

const ARCA_ALIAS = /^(?:arca(?:[_-][A-Za-z0-9_-]+)?|arclet[_-][A-Za-z0-9_-]+)\.ssh$/;
const FINGERPRINT = /^SHA256:[A-Za-z0-9+/]{43}$/;
const COMMAND_OPTIONS = { timeout: 1500, maxBuffer: 128 * 1024 };

/** Match public host keys to saved Arca connections without connecting over SSH. */
async function discoverArcaSshHost(
  fingerprints,
  { home = os.homedir(), run = promisify(execFile) } = {},
) {
  if (
    !Array.isArray(fingerprints) ||
    !fingerprints.length ||
    fingerprints.length > 16 ||
    !fingerprints.every((value) => typeof value === "string" && FINGERPRINT.test(value))
  ) {
    return null;
  }
  try {
    const aliases = (await fs.readdir(path.join(home, ".ssh"))).filter((name) =>
      ARCA_ALIAS.test(name),
    );
    // Never pick a partial result when the candidate set exceeds the discovery budget.
    if (aliases.length > 32) return null;
    const matches = await Promise.all(
      aliases.map(async (alias) => {
        try {
          const { stdout } = await run("/usr/bin/ssh", ["-G", alias], COMMAND_OPTIONS);
          const config = new Map(
            stdout
              .trim()
              .split("\n")
              .map((line) => {
                const space = line.indexOf(" ");
                return [line.slice(0, space), line.slice(space + 1).trim()];
              }),
          );
          const hostname = config.get("hostname");
          const port = config.get("port") || "22";
          if (!hostname) return null;
          const lookup =
            config.get("hostkeyalias") || (port === "22" ? hostname : `[${hostname}]:${port}`);
          const files = (config.get("userknownhostsfile") || "").match(/"[^"]*"|\S+/g) || [];
          if (files.length > 4) return null;
          const records = await Promise.all(
            files.map(async (file) => {
              let filename = file.replace(/^"|"$/g, "");
              if (filename.startsWith("~/")) filename = path.join(home, filename.slice(2));
              if (
                !path.isAbsolute(filename) ||
                filename.includes("%") ||
                filename === "/dev/null"
              ) {
                return "";
              }
              try {
                const result = await run(
                  "/usr/bin/ssh-keygen",
                  ["-F", lookup, "-f", filename],
                  COMMAND_OPTIONS,
                );
                return result.stdout;
              } catch {
                return "";
              }
            }),
          );
          const lines = records.join("\n").split("\n");
          if (lines.some((line) => /^\s*@revoked\s/.test(line))) return null;
          const matching = lines.some((line) => {
            if (/^\s*[#@]/.test(line)) return false;
            const fields = line.trim().split(/\s+/);
            if (fields.length < 3 || !/^[A-Za-z0-9+/]+={0,2}$/.test(fields[2])) return false;
            const digest = createHash("sha256")
              .update(Buffer.from(fields[2], "base64"))
              .digest("base64")
              .replace(/=+$/, "");
            return fingerprints.includes(`SHA256:${digest}`);
          });
          return matching ? alias : null;
        } catch {
          return null;
        }
      }),
    );
    const matchingAliases = matches.filter(Boolean);
    return matchingAliases.length === 1 ? matchingAliases[0] : null;
  } catch {
    return null;
  }
}

module.exports = { discoverArcaSshHost };
