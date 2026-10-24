// Start the Vite dev server over HTTPS (HTTP/2), creating a localhost cert on first run.
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const certDir = resolve(root, ".dev-certs");
const cert = process.env.OMNIGENT_DEV_HTTPS_CERT ?? resolve(certDir, "localhost.pem");
const key = process.env.OMNIGENT_DEV_HTTPS_KEY ?? resolve(certDir, "localhost-key.pem");

function hasCommand(command) {
  try {
    execFileSync(command, ["-help"], { stdio: "ignore" });
    return true;
  } catch (error) {
    return error.code !== "ENOENT";
  }
}

if (!existsSync(cert) || !existsSync(key)) {
  mkdirSync(certDir, { recursive: true });
  if (hasCommand("mkcert")) {
    execFileSync(
      "mkcert",
      ["-cert-file", cert, "-key-file", key, "localhost", "127.0.0.1", "::1"],
      { stdio: "inherit" },
    );
  } else {
    // A self-signed cert works, but Chrome skips caching for sites with cert errors.
    console.warn(
      "[dev-https] mkcert not found; creating a self-signed cert. Install mkcert for a trusted one.",
    );
    execFileSync(
      "openssl",
      [
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "825",
        "-subj",
        "/CN=localhost",
        "-addext",
        "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
        "-keyout",
        key,
        "-out",
        cert,
      ],
      { stdio: "inherit" },
    );
  }
}

// Run Vite's CLI in this process so signals and exit codes behave like `vite`.
process.env.OMNIGENT_DEV_HTTPS_CERT = cert;
process.env.OMNIGENT_DEV_HTTPS_KEY = key;
const viteBin = resolve(root, "node_modules/vite/bin/vite.js");
process.argv = [process.argv[0], viteBin, ...process.argv.slice(2)];
await import(pathToFileURL(viteBin).href);
