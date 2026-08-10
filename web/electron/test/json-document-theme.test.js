const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");

const {
  FORCE_LIGHT_JSON_DOCUMENT_SCRIPT,
  registerLightJsonDocumentTheme,
} = require("../src/json-document-theme");

function fakeDocument(contentType) {
  return {
    contentType,
    documentElement: { style: {} },
    body: { style: {} },
  };
}

function runThemeScript(contentType) {
  const document = fakeDocument(contentType);
  const changed = vm.runInNewContext(FORCE_LIGHT_JSON_DOCUMENT_SCRIPT, { document });
  return { changed, document };
}

describe("light JSON document theme", () => {
  for (const contentType of ["application/json", "text/json", "application/problem+json"]) {
    it(`forces ${contentType} onto a readable light palette`, () => {
      const { changed, document } = runThemeScript(contentType);

      assert.equal(changed, true);
      assert.deepEqual(
        { ...document.documentElement.style },
        { colorScheme: "only light", backgroundColor: "#fff", color: "#111" },
      );
      assert.deepEqual({ ...document.body.style }, { backgroundColor: "#fff", color: "#111" });
    });
  }

  it("leaves normal HTML documents untouched", () => {
    const { changed, document } = runThemeScript("text/html");

    assert.equal(changed, false);
    assert.deepEqual({ ...document.documentElement.style }, {});
    assert.deepEqual({ ...document.body.style }, {});
  });
});

describe("registerLightJsonDocumentTheme", () => {
  it("runs the JSON theme script on every dom-ready event", () => {
    const listeners = new Map();
    const executed = [];
    const webContents = {
      on: (event, listener) => listeners.set(event, listener),
      executeJavaScript: (script) => {
        executed.push(script);
        return Promise.resolve();
      },
    };

    registerLightJsonDocumentTheme(webContents);
    assert.deepEqual(executed, []);

    listeners.get("dom-ready")();
    listeners.get("dom-ready")();
    assert.deepEqual(executed, [
      FORCE_LIGHT_JSON_DOCUMENT_SCRIPT,
      FORCE_LIGHT_JSON_DOCUMENT_SCRIPT,
    ]);
  });
});
