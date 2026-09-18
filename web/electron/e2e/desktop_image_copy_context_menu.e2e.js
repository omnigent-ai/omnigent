// Right-clicking a PNG in the file preview must offer a way to copy the image
// on the desktop shell (Electron owns the menu there — no browser fallback).
//
// Journey (real desktop shell, real server + runner, real PNG on disk):
//   1. a session's working directory contains a PNG
//   2. open the session's Files (explore) view and click the PNG
//   3. the preview renders the image
//   4. right-click the image to copy it
//   5. EXPECTED: a context menu with a "Copy Image" item pops
//      BUG:      the shell's context-menu handler (attachContextMenu in
//                src/main.js) builds no items for image hit-tests, so no menu
//                appears at all — the user is left with only the zoom cursor.
//
// A native Electron menu is OS chrome that no page-level driver can see, so
// the test observes the app/native boundary instead: it records what
// Menu.buildFromTemplate is asked to pop while the REAL right-click flows
// through Chromium's hit test (params.mediaType === "image") into the real
// context-menu handler. Today nothing pops; after a fix the popped template
// must contain a copy-image item.
//
// Run from web/electron after building the SPA (see e2e/README.md):
//   OMNIGENT_PW_NO_SANDBOX=1 xvfb-run -a node --test e2e/desktop_image_copy_context_menu.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const {
  PYTHON_PATH,
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "image-copy-context-menu");

/** Same interpreter the harness uses for the server + mock LLM. */
const PYTHON = process.env.OMNIGENT_PYTHON || "python3";

const PNG_NAME = "preview-image.png";
// A real 480x320 PNG (indigo checker + white disc) so the preview shows an
// actual raster image — the exact kind of file the bug report is about.
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAeAAAAFACAIAAADrqjgsAAAKVElEQVR42u3dsVUdURBEQaLDVQAyiV2+Mnh4GLLgrP5uT0/NIYFL7ytP6O3X+59Hfj5+/33kR69evXqn9L4ZWK9evXoBbWC9evXqBbSB9erVC2gD69WrVy+gDaxXr15AG1ivXr16AW1gvXr16gW0D1qvXr2ANrBevXr1AtrAevXqBbSB9erVqxfQBtarV69eQOvVq1cvoA2sV69evYA2sF69egFtYL169eoFtIH16tULaAPr1atXL6ANrFevXr2A9kHr1asX0AbWq1evXkAbWK9evYA2sF69evUC2sB69erVC2i9evXqBbSB9erVqxfQBtarVy+gDaxXr169gDawXr16AW1gvXr16gW0gfXq1asX0AbWq1cvoA2sV69evYA2sF69egFtYL169eoFtIH16tWrF9B69erVC2gD69WrVy+gDaxXr95KoP2i9erVqzezF9B69erVC2gD69WrVy+gDaxXr15AG1ivXr16AW1gvXr1AtrAevXq1QtoA+vVq1cvoH3QevXqBbSB9erVqxfQBtarVy+gDaxXr169gDawXr169QJar169egFtYL169eoFtIH16tULaAPr1atXL6ANrFevXkAbWK9evXoBbWC9evXqBbQPWq9evYA2sF69evUC2sB69eoFtIH16tWrF9AG1nv955xjX72ANrDeW3vP/z776gW0gfVGcPxqsu2rF9AGbu49SWdfvYA28PbeM+HsqxfQBl7Ue2aeffUC2sC1vafl7KsX0Abu6T2NZ1+9gDbw4N6z43zPegFt4Em9Z9/5nvUC2sDpvWf3+Z71AtrAib3H3c607xnQBtaL5lCmfc+ANrBeNIcy7XsGtIH10jnUaN8zoA2sF82hTPuey4H2i9Y7908alf17cd+z3n9+AK2XzoON9j0D2sB66RxqtO8Z0Abe3svTWKZ9z4A28OpejCYb7XsGtIH911Mu1GjfM6ANvLGXmyOY9j0D2sDrenE5xWjfM6AN7D9vdaFG+54BbeBFvYicZbTvGdAG3tILx3FG+54BbeAVvVicaLTvGdAG7u8F4lCjfc+ANnB5LwrnGu17BrSBm3shONpo3zOgDVzbi7/pRvueAW3gzl7wFRjtewa0gQt7kddhtO8Z0AZu64VdjdG+Z0AbuKoXc01G+54B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";

/** GET a URL and parse the JSON body (test-local; the harness only has a status probe). */
function httpGetJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let body = "";
      res.on("data", (d) => {
        body += d;
      });
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode, json: JSON.parse(body) });
        } catch (err) {
          reject(err);
        }
      });
    });
    req.on("error", reject);
    req.setTimeout(5000, () => req.destroy(new Error("timeout")));
  });
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

// The agent bundle mirrors tests/e2e_ui/conftest.py's _TEST_AGENT_YAML /
// _build_hello_world_bundle: a caller_process os_env gives the session a real
// local working directory the Files tab can browse.
const SEED_SESSION_PY = String.raw`
import io, json, sys, tarfile, gzip, urllib.request

server, runner_id = sys.argv[1], sys.argv[2]
yaml = (
    "name: hello_world\n"
    "prompt: You are a friendly assistant. Say hello and answer questions.\n"
    "\n"
    "executor:\n"
    "  model: gpt-4o-mini\n"
    "  harness: openai-agents\n"
    "\n"
    "os_env:\n"
    "  type: caller_process\n"
    "  cwd: .\n"
    "  sandbox:\n"
    "    type: none\n"
)
buf = io.BytesIO()
with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
    with tarfile.open(fileobj=gz, mode="w") as tar:
        data = yaml.encode()
        info = tarfile.TarInfo(name="hello_world.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
bundle = buf.getvalue()

boundary = "imagecopymenuboundary"
body = io.BytesIO()
def w(part):
    body.write(part if isinstance(part, bytes) else part.encode())
w(f"--{boundary}\r\n")
w('Content-Disposition: form-data; name="metadata"\r\n\r\n{}\r\n')
w(f"--{boundary}\r\n")
w('Content-Disposition: form-data; name="bundle"; filename="agent.tar.gz"\r\n')
w("Content-Type: application/gzip\r\n\r\n")
w(bundle)
w("\r\n")
w(f"--{boundary}--\r\n")
req = urllib.request.Request(
    f"{server}/v1/sessions",
    data=body.getvalue(),
    method="POST",
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
)
sid = json.load(urllib.request.urlopen(req, timeout=30))["session_id"]
patch = urllib.request.Request(
    f"{server}/v1/sessions/{sid}",
    data=json.dumps({"runner_id": runner_id}).encode(),
    method="PATCH",
    headers={"Content-Type": "application/json"},
)
urllib.request.urlopen(patch, timeout=10).read()
print(sid)
`;

describe(
  "desktop shell — copy a previewed PNG via right-click",
  { skip: !deps.ok && `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let runnerProc;
    let runnerOut;
    let osEnvRoot;
    let runnerId;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "desktop-image-copy-"));
      osEnvRoot = path.join(tmpDir, "os-env-root");
      fs.mkdirSync(osEnvRoot, { recursive: true });

      // Mirror tests/e2e_ui/conftest.py's live_server: a token-bound sibling
      // runner, with the server told to accept exactly that runner's tunnel.
      const binding = require("node:crypto").randomBytes(32).toString("base64url");
      const idOut = spawnSync(
        PYTHON,
        [
          "-c",
          "import sys; from omnigent.runner.identity import token_bound_runner_id; " +
            "print(token_bound_runner_id(sys.argv[1]))",
          binding,
        ],
        { env: { ...process.env, PYTHONPATH: PYTHON_PATH }, encoding: "utf8" },
      );
      assert.equal(idOut.status, 0, `token_bound_runner_id failed: ${idOut.stderr}`);
      runnerId = idOut.stdout.trim();

      server = await spawnServer(tmpDir, {
        serverEnv: { OMNIGENT_RUNNER_TUNNEL_TOKEN: binding },
      });

      // Strip ambient runner/host env (same reason as the harness), then set
      // exactly the vars a sibling runner needs. OMNIGENT_RUNNER_OS_ENV_ROOT
      // pins per-session workspaces under our scratch dir so the test can put a
      // real PNG into the session's working directory.
      const cleanEnv = Object.fromEntries(
        Object.entries(process.env).filter(
          ([key]) => !key.startsWith("OMNIGENT_RUNNER_") && !key.startsWith("OMNIGENT_HOST_"),
        ),
      );
      runnerOut = fs.openSync(path.join(tmpDir, "runner.log"), "w");
      runnerProc = spawn(PYTHON, ["-m", "omnigent.runner._entry"], {
        env: {
          ...cleanEnv,
          PYTHONPATH: PYTHON_PATH,
          OMNIGENT_RUNNER_ID: runnerId,
          OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN: binding,
          OMNIGENT_RUNNER_PARENT_PID: String(process.pid),
          RUNNER_SERVER_URL: server.serverUrl,
          OMNIGENT_RUNNER_OS_ENV_ROOT: osEnvRoot,
          OPENAI_BASE_URL: `${server.mockUrl}/v1`,
          OPENAI_API_KEY: "mock-key",
        },
        stdio: ["ignore", runnerOut, runnerOut],
      });

      // Wait until the server can actually route through the runner.
      const deadline = Date.now() + 60_000;
      let last;
      for (;;) {
        try {
          // oxlint-disable-next-line no-await-in-loop -- readiness poll is sequential
          const r = await httpGetJson(`${server.serverUrl}/v1/runners/${runnerId}/status`);
          if (r.status === 200 && r.json.online === true) break;
          last = `HTTP ${r.status}: ${JSON.stringify(r.json)}`;
        } catch (err) {
          last = String(err);
        }
        if (Date.now() > deadline) {
          const log = fs.readFileSync(path.join(tmpDir, "runner.log"), "utf8");
          throw new Error(`runner never came online: ${last || "not polled"}\n${log.slice(-3000)}`);
        }
        // oxlint-disable-next-line no-await-in-loop -- readiness poll is sequential
        await sleep(500);
      }
    });

    after(async () => {
      if (runnerProc && runnerProc.exitCode === null) runnerProc.kill("SIGTERM");
      if (runnerOut !== undefined) {
        try {
          fs.closeSync(runnerOut);
        } catch {
          /* already closed */
        }
      }
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it(
      "right-clicking the previewed PNG pops a context menu with a copy-image item",
      { timeout: 240_000 },
      async () => {
        // Seed a runner-bound session, then drop a real PNG into its working
        // directory (OMNIGENT_RUNNER_OS_ENV_ROOT/<sid>/workspace — the runner's
        // per-session default when no runner workspace is set).
        const seeded = spawnSync(PYTHON, ["-c", SEED_SESSION_PY, server.serverUrl, runnerId], {
          env: { ...process.env, PYTHONPATH: PYTHON_PATH },
          encoding: "utf8",
        });
        assert.equal(seeded.status, 0, `session seeding failed: ${seeded.stderr}`);
        const sessionId = seeded.stdout.trim();
        const workspace = path.join(osEnvRoot, sessionId, "workspace");
        fs.mkdirSync(workspace, { recursive: true });
        fs.writeFileSync(path.join(workspace, PNG_NAME), Buffer.from(PNG_BASE64, "base64"));

        const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
          recordDir: RECORD_DIR,
          serverUrl: server.serverUrl,
        });
        let observed = null;
        try {
          // The pre-seeded server boots the shell straight onto the SPA home.
          await window
            .getByText("What should we build?")
            .waitFor({ state: "visible", timeout: 30_000 });

          // Open the session's Files (explore) view and the PNG's preview.
          await window.evaluate((u) => {
            window.location.href = u;
          }, `${server.serverUrl}/c/${sessionId}?view=explore`);
          const fileButton = window.getByRole("button", { name: /^preview-image\.png\b/ });
          await fileButton.waitFor({ state: "visible", timeout: 30_000 });
          await fileButton.click();
          const img = window.locator(`[data-testid="file-viewer"]:visible img[alt="${PNG_NAME}"]`);
          await img.waitFor({ state: "visible", timeout: 15_000 });
          await window.waitForFunction(
            (el) => el.complete && el.naturalWidth > 0,
            await img.elementHandle(),
            { timeout: 15_000 },
          );

          // Record, at the app/native boundary, what the shell pops for the
          // right-click: capture the context-menu hit-test params (proves the
          // click reached the handler as an image) and every Menu template built
          // afterwards. popup() is stubbed — an open native menu can be neither
          // observed nor dismissed under automation.
          await electronApp.evaluate(({ Menu, BrowserWindow }) => {
            const state = { menus: [], params: [] };
            globalThis.imageMenuState = state;
            Menu.buildFromTemplate = (template) => {
              state.menus.push(template.map((item) => item.label || item.role || item.type || ""));
              return { popup: () => {}, closePopup: () => {} };
            };
            for (const win of BrowserWindow.getAllWindows()) {
              win.webContents.on("context-menu", (_event, params) => {
                state.params.push({
                  mediaType: params.mediaType,
                  hasImageContents: params.hasImageContents,
                  srcURL: params.srcURL,
                });
              });
            }
          });

          // The user action under test: right-click the previewed image.
          await img.click({ button: "right" });

          // Give the context-menu event time to cross renderer → main.
          const deadline = Date.now() + 10_000;
          for (;;) {
            // oxlint-disable-next-line no-await-in-loop -- event-arrival poll is sequential
            observed = await electronApp.evaluate(() => globalThis.imageMenuState);
            if (observed.params.length > 0) break;
            if (Date.now() > deadline) break;
            // oxlint-disable-next-line no-await-in-loop -- event-arrival poll is sequential
            await sleep(250);
          }
          // Hold the final state on screen so the recording shows the (non-)result.
          await window.waitForTimeout(3_000);
        } finally {
          await electronApp.close();
          await stopDisplayCapture();
          saveRecording(RECORD_DIR, "image-right-click-copy");
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }

        // Preconditions: the right-click really reached the shell's handler and
        // Chromium hit-tested the image (else the assertions below would be
        // vacuous).
        assert.ok(
          observed && observed.params.length > 0,
          "the right-click never reached the shell's context-menu handler",
        );
        assert.equal(
          observed.params[0].mediaType,
          "image",
          `expected an image hit-test, got: ${JSON.stringify(observed.params)}`,
        );

        // The bug: no menu pops at all for an image right-click, so there is no
        // way to copy the previewed PNG. After a fix, the popped template must
        // offer a copy-image item.
        assert.ok(
          observed.menus.length > 0,
          "right-clicking the previewed PNG opened NO context menu — " +
            "attachContextMenu (web/electron/src/main.js) builds no items for " +
            "image hit-tests, so the image cannot be copied from the preview",
        );
        const hasCopyImage = observed.menus.some((labels) =>
          labels.some((label) => /copy.*image/i.test(String(label))),
        );
        assert.ok(
          hasCopyImage,
          `the context menu over the previewed PNG offers no ` +
            `copy-image item (popped menus: ${JSON.stringify(observed.menus)})`,
        );
      },
    );
  },
);
