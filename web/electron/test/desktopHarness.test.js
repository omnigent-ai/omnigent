// saveRecording must hand a viewer the clip that actually shows the app: the
// composited display capture (which includes WebContentsView content) is the
// primary clip; Playwright's per-page clips are context, never the primary
// when a display capture exists.

"use strict";

const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  launchDesktop,
  saveRecording,
  startPrivateDisplay,
  pollUntil,
  displaySocketPath,
  xvfbAvailable,
} = require("../e2e/desktopHarness");

/** PIDs of Xvfb processes whose parent is this test process (Linux /proc). */
function ownedXvfbPids() {
  return fs.readdirSync("/proc").filter((name) => {
    if (!/^\d+$/.test(name)) return false;
    try {
      const match = /^\d+ \((.*)\) \S+ (\d+) /.exec(fs.readFileSync(`/proc/${name}/stat`, "utf8"));
      return match !== null && match[1] === "Xvfb" && Number(match[2]) === process.pid;
    } catch {
      return false; // the process exited between readdir and read
    }
  });
}

const hasXvfb = xvfbAvailable();

describe("saveRecording", () => {
  let dir;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-save-recording-"));
  });

  afterEach(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it("promotes the composited display capture to the primary clip", () => {
    // The per-page clip of the shell window is typically the LARGEST file, but
    // it omits the browser-view content; the display capture must still win.
    fs.writeFileSync(path.join(dir, "display@1.webm"), Buffer.alloc(64));
    fs.writeFileSync(path.join(dir, "page@abcd1234.webm"), Buffer.alloc(4096));
    const saved = saveRecording(dir, "clip");
    assert.equal(saved.length, 2);
    assert.equal(path.basename(saved[0]), "clip.webm");
    // The primary is the display capture (64 bytes), not the larger page clip.
    assert.equal(fs.statSync(saved[0]).size, 64);
    assert.equal(path.basename(saved[1]), "clip-2.webm");
    assert.equal(fs.statSync(saved[1]).size, 4096);
  });

  it("falls back to per-page clips, largest first, without a display capture", () => {
    fs.writeFileSync(path.join(dir, "page@a.webm"), Buffer.alloc(300));
    fs.writeFileSync(path.join(dir, "page@b.webm"), Buffer.alloc(500));
    const saved = saveRecording(dir, "clip");
    assert.equal(saved.length, 2);
    assert.equal(path.basename(saved[0]), "clip.webm");
    assert.equal(fs.statSync(saved[0]).size, 500);
    assert.equal(fs.statSync(saved[1]).size, 300);
  });

  it("ignores zero-byte clips left by a recorder that produced nothing", () => {
    fs.writeFileSync(path.join(dir, "display@1.webm"), Buffer.alloc(0));
    fs.writeFileSync(path.join(dir, "page@a.webm"), Buffer.alloc(100));
    const saved = saveRecording(dir, "clip");
    assert.equal(saved.length, 1);
    assert.equal(fs.statSync(saved[0]).size, 100);
  });

  it("returns empty when nothing was recorded", () => {
    assert.deepEqual(saveRecording(dir, "clip"), []);
  });
});

describe("displaySocketPath", () => {
  it("maps a display to its socket and ignores a screen suffix", () => {
    assert.equal(displaySocketPath(":99"), "/tmp/.X11-unix/X99");
    assert.equal(displaySocketPath(":99.0"), "/tmp/.X11-unix/X99");
  });
});

describe("startPrivateDisplay", () => {
  let savedDisplay;

  beforeEach(() => {
    savedDisplay = process.env.DISPLAY;
  });

  afterEach(() => {
    if (savedDisplay === undefined) delete process.env.DISPLAY;
    else process.env.DISPLAY = savedDisplay;
  });

  it("leaves an existing display alone", async () => {
    process.env.DISPLAY = ":42";
    assert.equal(await startPrivateDisplay(), null);
  });

  it(
    "starts its own Xvfb when Linux has no display and stops it on request",
    { skip: hasXvfb ? false : "needs Linux with Xvfb installed" },
    async () => {
      delete process.env.DISPLAY;
      const owned = await startPrivateDisplay();
      const socket = displaySocketPath(owned.display);
      try {
        assert.match(owned.display, /^:\d+$/);
        assert.ok(fs.existsSync(socket), `no X socket at ${socket}`);
      } finally {
        await owned.stop();
      }
      // Xvfb unlinks its socket on SIGTERM shortly after exiting.
      assert.ok(await pollUntil(() => !fs.existsSync(socket), 3_000), `X socket left at ${socket}`);
    },
  );
});

describe("launchDesktop setup failure", () => {
  const deps = desktopDepsAvailable();
  let savedDisplay;
  let dir;

  beforeEach(() => {
    savedDisplay = process.env.DISPLAY;
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-launch-failure-"));
  });

  afterEach(() => {
    if (savedDisplay === undefined) delete process.env.DISPLAY;
    else process.env.DISPLAY = savedDisplay;
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it(
    "releases the owned Xvfb when setup fails before Electron launches",
    {
      skip: hasXvfb && deps.ok ? false : "needs Linux with Xvfb, electron and playwright installed",
    },
    async () => {
      delete process.env.DISPLAY;
      assert.deepEqual(ownedXvfbPids(), [], "an Xvfb child already exists");
      // A regular file where the record dir must go makes mkdirSync throw.
      const blocker = path.join(dir, "not-a-directory");
      fs.writeFileSync(blocker, "");
      await assert.rejects(launchDesktop({ recordDir: path.join(blocker, "recordings") }));
      assert.ok(
        await pollUntil(() => ownedXvfbPids().length === 0, 3_000),
        `Xvfb still running: ${ownedXvfbPids()}`,
      );
    },
  );
});
