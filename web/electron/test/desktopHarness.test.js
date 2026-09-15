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

const { saveRecording } = require("../e2e/desktopHarness");

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
