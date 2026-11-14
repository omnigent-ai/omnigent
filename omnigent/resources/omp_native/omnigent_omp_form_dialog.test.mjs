import { afterAll, expect, test } from "bun:test";
import { mkdtempSync, copyFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const packageRoot = process.env.OMNIGENT_TEST_OMP_PACKAGE_ROOT;
if (!packageRoot) throw new Error("Set OMNIGENT_TEST_OMP_PACKAGE_ROOT to the installed OMP package");
const parse = await import(Bun.resolveSync("@oh-my-pi/pi-tui/html-form/parse", packageRoot));
const data = await import(Bun.resolveSync("@oh-my-pi/pi-tui/html-form/data", packageRoot));
const here = dirname(fileURLToPath(import.meta.url));
const stage = mkdtempSync(join(tmpdir(), "omp-native-form-"));
copyFileSync(join(here, "omnigent_omp_native_extension.mjs"), join(stage, "adapter.mjs"));
copyFileSync(join(here, "../pi_native/omnigent_pi_native_extension.js"), join(stage, "omnigent_pi_native_extension.cjs"));
const { createOmpFormDialog } = await import(pathToFileURL(join(stage, "adapter.mjs")).href);
const dialog = createOmpFormDialog({ ...parse, ...data });
afterAll(() => rmSync(stage, { recursive: true, force: true }));

const HTML = `<form><script>NEVER_EXECUTE_THIS()</script><fieldset><legend>Deployment</legend>
<input type="hidden" name="csrf" value="native-only-secret">
<label>Project <input name="project" value="sample" required></label>
<label>Workers <input name="workers" type="number" min="1" max="8" value="2" required></label>
<label>Rate <input name="rate" type="range" min="0" max="1" value="0.5"></label>
<label>Email <input name="email" type="email"></label>
<label>URL <input name="url" type="url"></label>
<label>Password <input name="password" type="password"></label>
<label>Date <input name="date" type="date" value="2026-11-13"></label>
<label>Notes <textarea name="notes">line one\nline two</textarea></label>
<p>Region</p><label><input type="radio" name="region" value="west" checked> Same label</label>
<label><input type="radio" name="region" value="east"> Same label</label>
<p>Features</p><label><input type="checkbox" name="features" value="logs" checked>Logs</label>
<label><input type="checkbox" name="features" value="metrics">Metrics</label>
<label><input type="checkbox" name="features" value="blocked" checked disabled>Unavailable</label>
<label>Database <select name="database"><option value="sqlite" selected>SQLite</option><option value="pg">Postgres</option><option value="unavailable" disabled>Unavailable</option></select></label>
<label>Languages <select name="languages" multiple><option value="py" selected>Python</option><option value="js">JS</option></select></label>
<label><input type="checkbox" name="agree" value="yes" checked>Accept terms</label>
</fieldset></form>`;
const form = parse.parseHtmlForm(HTML);
const key = (name) => `f${form.fields.findIndex((field) => field.name === name)}`;
const defaults = (request) => Object.fromEntries(Object.entries(request.requestedSchema.properties).map(([name, prop]) => [name, prop.default]));

test("real OMP parser preserves typed controls, defaults, hidden values, disabled options and duplicate labels", async () => {
  let request;
  const result = await dialog(HTML, {}, async (req) => {
    request = req;
    const values = defaults(req);
    values[key("project")] = "renamed";
    values[key("workers")] = 4;
    values[key("rate")] = 0.75;
    values[key("email")] = "person@example.test";
    values[key("url")] = "https://example.test/";
    values[key("password")] = "a secret";
    values[key("notes")] = "keep\nall lines\n";
    values[key("region")] = "1";
    values[`${key("features")}_0`] = false;
    values[`${key("features")}_1`] = true;
    values[`${key("features")}_2`] = true; // Forged disabled selection is ignored by native choiceValue.
    values[key("database")] = "1";
    values[`${key("languages")}_0`] = false;
    values[`${key("languages")}_1`] = true;
    return { value: values };
  });
  expect(result.data).toEqual({ csrf: "native-only-secret", project: "renamed", workers: 4, rate: 0.75, email: "person@example.test", url: "https://example.test/", password: "a secret", date: "2026-11-13", notes: "keep\nall lines\n", region: "east", features: ["metrics"], database: "pg", languages: ["js"], agree: "yes" });
  expect(request.method).toBe("formDialog");
  expect(JSON.stringify(request)).not.toContain("NEVER_EXECUTE_THIS");
  expect(JSON.stringify(request)).not.toContain("native-only-secret");
  expect(request).not.toHaveProperty("html");
  const properties = request.requestedSchema.properties;
  expect(properties[key("notes")]["x-multiline"]).toBe(true);
  expect(properties[key("password")]["x-input-type"]).toBe("password");
  expect(properties[key("workers")].type).toBe("number");
  expect(properties[`${key("features")}_2`]["x-disabled"]).toBe(true);
  expect(properties[key("database")]["x-disabled-enum"]).toEqual(["2"]);
  expect(properties[key("region")].enum).toEqual(["0", "1"]);
  expect(properties[key("region")].enumNames[0]).toBe(properties[key("region")].enumNames[1]);
});

test("real native Other and empty text semantics survive form transport", async () => {
  const result = await dialog(HTML, {}, async (req) => ({ value: {
    ...defaults(req), [key("notes")]: "", [key("email")]: "  ",
    [`${key("features")}_other`]: " custom-feature ", [`${key("region")}_other`]: " custom-region ",
    [`${key("agree")}_0`]: false,
  } }));
  expect(result.data.notes).toBe("");
  expect(result.data.email).toBe("  ");
  expect(result.data.features).toEqual(["logs", "custom-feature"]);
  expect(result.data.region).toBe("custom-region");
  expect(result.data).not.toHaveProperty("agree");
});

test("native field validation reasks a whole corrected form, retaining submitted values", async () => {
  let calls = 0;
  const result = await dialog(HTML, {}, async (req) => {
    calls += 1;
    if (calls === 1) return { value: { ...defaults(req), [key("workers")]: 99 } };
    expect(req.requestedSchema.properties[key("workers")].description).toBe("Must be at most 8");
    expect(req.requestedSchema.properties[key("workers")].default).toBe(99);
    return { value: { ...defaults(req), [key("workers")]: 3 } };
  });
  expect(calls).toBe(2);
  expect(result.data.workers).toBe(3);
});

test("invalid answers cannot create an unbounded reask loop", async () => {
  let calls = 0;
  await expect(dialog(HTML, {}, async (req) => {
    calls += 1;
    return { value: { ...defaults(req), [key("project")]: "" } };
  })).rejects.toThrow("native field validation");
  expect(calls).toBe(4);
});

test("cancel and abort never submit defaults, even at the deadline", async () => {
  const clock = Date.now;
  try {
    Date.now = () => 100;
    const cancelled = await dialog(HTML, { timeout: 10 }, async () => {
      Date.now = () => 111;
      return { cancelled: true, timedOut: false };
    });
    expect(cancelled).toBeUndefined();
  } finally {
    Date.now = clock;
  }
  const controller = new AbortController();
  const pending = dialog(HTML, { signal: controller.signal }, async () => new Promise(() => {}));
  controller.abort();
  expect(await pending).toBeUndefined();
});

test("a true native deadline returns native defaults; a transport cap without a native deadline cancels", async () => {
  const clock = Date.now;
  let timeouts = 0;
  try {
    Date.now = () => 100;
    const result = await dialog(HTML, { timeout: 10, onTimeout: () => { timeouts += 1; } }, async () => {
      Date.now = () => 111;
      return { cancelled: true, timedOut: true };
    });
    expect(result).toEqual({ data: data.defaultFormData(form), timedOut: true });
    expect(timeouts).toBe(1);
  } finally {
    Date.now = clock;
  }
  expect(await dialog(HTML, {}, async () => ({ cancelled: true, timedOut: true }))).toBeUndefined();
});
