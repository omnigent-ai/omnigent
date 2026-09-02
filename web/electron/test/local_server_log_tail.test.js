// Tests for the setup-terminal log tail helpers (src/omnigent_cli.js), run with
// `node --test`. Focus: the line splitter's partial-line buffering across chunk
// boundaries (the classic tail bug — never emit a half line), CRLF trimming,
// and ANSI stripping so streamed uvicorn/app logs render as plain text.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { makeLineSplitter, stripAnsi } = require("../src/omnigent_cli");

describe("makeLineSplitter", () => {
  it("emits whole lines and buffers a trailing partial across chunks", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    s.push("hello\nwor");
    assert.deepEqual(out, ["hello"]); // "wor" is buffered, not emitted
    s.push("ld\n");
    assert.deepEqual(out, ["hello", "world"]);
  });

  it("splits a line that arrives one byte at a time", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    for (const ch of "ab\ncd\n") s.push(ch);
    assert.deepEqual(out, ["ab", "cd"]);
  });

  it("handles multiple newlines in one chunk", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    s.push("a\nb\nc\n");
    assert.deepEqual(out, ["a", "b", "c"]);
  });

  it("trims a trailing CR (CRLF logs)", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    s.push("line\r\nnext\r\n");
    assert.deepEqual(out, ["line", "next"]);
  });

  it("does not emit a final unterminated line", () => {
    const out = [];
    const s = makeLineSplitter((line) => out.push(line));
    s.push("no newline here");
    assert.deepEqual(out, []);
  });
});

describe("stripAnsi", () => {
  it("removes SGR color codes, keeps text", () => {
    assert.equal(stripAnsi("\x1b[32mINFO\x1b[0m ready"), "INFO ready");
  });
  it("leaves plain text untouched", () => {
    assert.equal(stripAnsi("plain line"), "plain line");
  });
});
