const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { createSshHostStore } = require("../src/sshHosts");

describe("createSshHostStore", () => {
  let directory;
  let file;

  beforeEach(() => {
    directory = fs.mkdtempSync(path.join(os.tmpdir(), "ssh-hosts-"));
    file = path.join(directory, "ssh-hosts.json");
  });

  afterEach(() => {
    fs.rmSync(directory, { recursive: true, force: true });
  });

  it("keeps each server's hosts apart, across restarts, in a private file", () => {
    const hostId = "0123456789abcdef0123456789abcdef";
    createSshHostStore(() => file).record("https://a.example/", hostId, "my-box");

    const reloaded = createSshHostStore(() => file);

    assert.equal(reloaded.destination("https://a.example/", hostId), "my-box");
    assert.equal(
      reloaded.destination("https://a.example/", "01234567-89ab-cdef-0123-456789ABCDEF"),
      "my-box",
      "a dashed id finds the same host",
    );
    assert.equal(reloaded.destination("https://b.example/", hostId), null);
    assert.equal(reloaded.destination("https://a.example/", "__proto__"), null);
    if (process.platform !== "win32") {
      assert.equal((fs.statSync(file).mode & 0o777).toString(8), "600");
    }
  });

  it("records a dashed id under the same key a lookup uses", () => {
    const store = createSshHostStore(() => file);

    store.record("https://a.example/", "01234567-89ab-cdef-0123-456789ABCDEF", "my-box");

    assert.equal(
      store.destination("https://a.example/", "0123456789abcdef0123456789abcdef"),
      "my-box",
    );
  });

  it("ignores a destination that isn't a valid ssh destination", () => {
    fs.writeFileSync(file, JSON.stringify({ "https://a.example/": { h: "-oProxyCommand=x" } }));

    assert.equal(createSshHostStore(() => file).destination("https://a.example/", "h"), null);
  });

  it("replaces a server's value that isn't a record instead of spreading it", () => {
    fs.writeFileSync(file, JSON.stringify({ "https://a.example/": "abc" }));

    createSshHostStore(() => file).record(
      "https://a.example/",
      "0123456789abcdef0123456789abcdef",
      "my-box",
    );

    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")), {
      "https://a.example/": { "0123456789abcdef0123456789abcdef": "my-box" },
    });
  });

  it("reads nothing from a missing, corrupt or non-object file", () => {
    assert.equal(createSshHostStore(() => file).destination("https://a.example/", "h"), null);
    for (const contents of ["{", "[]"]) {
      fs.writeFileSync(file, contents);
      assert.equal(
        createSshHostStore(() => file).destination("https://a.example/", "h"),
        null,
        contents,
      );
    }
  });

  it("keeps the previous record when a write fails, and leaves no temp file", () => {
    const hostId = "0123456789abcdef0123456789abcdef";
    const otherId = "fedcba9876543210fedcba9876543210";
    const store = createSshHostStore(() => file);
    store.record("https://a.example/", hostId, "my-box");
    // A directory where the record should be makes the rename fail.
    fs.rmSync(file);
    fs.mkdirSync(file);

    assert.throws(() => store.record("https://a.example/", otherId, "other-box"));

    assert.equal(store.destination("https://a.example/", hostId), "my-box");
    assert.equal(store.destination("https://a.example/", otherId), null);
    assert.deepEqual(
      fs.readdirSync(directory).filter((name) => name.endsWith(".tmp")),
      [],
    );
  });
});
