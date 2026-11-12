"use strict";

// The SSH connect console page's script, rendered in jsdom. The flow is tested
// in ssh_connect_window.test.ts.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SERVER = "https://example.cloud.databricks.com/omnigent";

function renderConsole(t) {
  const dom = new JSDOM(
    fs.readFileSync(path.join(__dirname, "../ssh-connect/index.html"), "utf8"),
    { runScripts: "outside-only" },
  );
  t.after(() => dom.window.close());
  const page = {
    document: dom.window.document,
    events: {},
    answers: [],
    confirmed: 0,
    refits: 0,
    resizeCallbacks: new Map(),
  };
  dom.window.sshConnect = {
    confirm: () => {
      page.confirmed += 1;
    },
    cancel() {},
    answer: (payload) => page.answers.push(payload),
    resizeToContent() {},
  };
  for (const event of ["Init", "Started", "Status", "Prompt", "PromptClosed", "Output", "Done"]) {
    dom.window.sshConnect[`on${event}`] = (listener) => {
      page.events[event] = listener;
    };
  }
  dom.window.Terminal = class Terminal {
    loadAddon() {}
    open() {}
    write() {}
  };
  dom.window.FitAddon = {
    FitAddon: class FitAddon {
      fit() {
        page.refits += 1;
      }
    },
  };
  dom.window.ResizeObserver = class ResizeObserver {
    constructor(callback) {
      this.callback = callback;
    }
    observe(element) {
      page.resizeCallbacks.set(element, this.callback);
    }
  };
  dom.window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  dom.window.cancelAnimationFrame = () => {};
  dom.window.eval(fs.readFileSync(path.join(__dirname, "../ssh-connect/console.js"), "utf8"));
  return page;
}

describe("SSH connect console page", () => {
  it("shows the machine and commands as text, and answers secret, trust and cancel prompts", (t) => {
    const { document, events, answers } = renderConsole(t);
    events.Init({
      serverUrl: SERVER,
      destination: "<b>my-ec2</b>",
      commands: ["ssh my-ec2 omnigent login 'x'", "ssh my-ec2 omnigent host"],
    });
    assert.equal(document.getElementById("destination").textContent, "<b>my-ec2</b>");
    assert.equal(document.querySelectorAll("#commands div").length, 2);
    assert.equal(document.querySelector("b"), null);

    const promptEl = document.getElementById("prompt");
    const input = document.getElementById("prompt-input");
    events.Prompt({ id: 1, kind: "secret", text: "pat@my-ec2's password: " });
    assert.equal(promptEl.hidden, false);
    assert.equal(input.type, "password");
    input.value = "pw";
    promptEl.dispatchEvent(new document.defaultView.Event("submit", { cancelable: true }));
    assert.equal(promptEl.hidden, true);

    events.Prompt({ id: 2, kind: "confirm", text: "ED25519 key fingerprint is SHA256:abc." });
    assert.equal(input.hidden, true);
    assert.equal(document.getElementById("prompt-ok").textContent, "Trust");
    promptEl.dispatchEvent(new document.defaultView.Event("submit", { cancelable: true }));

    events.Prompt({ id: 3, kind: "text", text: "Which project?" });
    document.getElementById("prompt-cancel").click();

    // ssh gave up on prompt 4; a stale close for another prompt changes nothing.
    events.Prompt({ id: 4, kind: "secret", text: "Passphrase: " });
    events.PromptClosed({ id: 3 });
    assert.equal(promptEl.hidden, false);
    events.PromptClosed({ id: 4 });
    assert.equal(promptEl.hidden, true);
    document.getElementById("prompt-cancel").click();

    // Built in the page's realm, so compare plain copies.
    assert.deepEqual(JSON.parse(JSON.stringify(answers)), [
      { id: 1, answer: "pw" },
      { id: 2, answer: "yes" },
      { id: 3, cancel: true },
    ]);
    assert.equal(input.value, "");
  });

  it("shows progress and the final outcome", (t) => {
    const page = renderConsole(t);
    const { document, events } = page;
    events.Init({ serverUrl: SERVER, destination: "my-ec2", commands: [] });
    events.Started({});
    // A prompt shrinks the output panel without resizing the window; the terminal must refit.
    const refitsBefore = page.refits;
    page.resizeCallbacks.get(document.getElementById("terminal"))?.();
    assert.equal(page.refits, refitsBefore + 1);
    events.Status({ step: "waiting-for-browser" });
    assert.match(
      document.getElementById("status").textContent,
      /Finish signing in in your browser/,
    );
    events.Done({ ok: false, error: "my-ec2 didn't accept your sign-in." });
    assert.equal(document.getElementById("status").dataset.kind, "error");
    assert.equal(document.getElementById("cancel").textContent, "Close");
  });
});
