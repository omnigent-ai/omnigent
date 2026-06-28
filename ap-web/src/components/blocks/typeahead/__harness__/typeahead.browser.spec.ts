// Browser (Playwright) verification of predictive local echo in a REAL browser.
// Loads the harness (xterm + TypeAheadAddon + a fake PTY that echoes after a
// simulated RTT) and asserts the user-visible behavior that unit tests in jsdom
// cannot: a typed character is painted DIMMED immediately (before the echo
// returns) and then becomes solid (non-dim) once the server echo reconciles.
//
// Run: npx playwright test (see playwright.config.ts — it starts `vite`).

/* eslint-disable no-underscore-dangle -- the harness exposes a `window.__ta`
   control API and the test reads the addon's `_timeline`; both are test-only. */
import { expect, test } from "@playwright/test";

const HARNESS = "/src/components/blocks/typeahead/__harness__/harness.html";

test.beforeEach(async ({ page }) => {
  await page.goto(HARNESS);
  await page.waitForFunction(() => window.__ta?.ready === true);
});

test("predicts a typed character immediately (dimmed), then confirms it (solid)", async ({
  page,
}) => {
  const rtt = await page.evaluate(() => window.__ta.rttMs);

  // The prompt "$ " occupies cols 0-1, so typed input lands at col 2 on row 0.
  // First char on a line is held tentative (epoch model), so prime the line:
  // type "a", let it round-trip and confirm, so the line is proven to echo.
  await page.evaluate(() => window.__ta.type("a"));
  await page.waitForTimeout(rtt + 100);

  // Now type "b": it must be PREDICTED (painted dim) BEFORE the echo returns.
  await page.evaluate(() => window.__ta.type("b"));

  // Immediately (well within the RTT window) the glyph is on screen AND dim.
  const predicted = await page.evaluate(() => ({
    text: window.__ta.rowText(0),
    bDim: window.__ta.cellIsDim(3, 0), // col 3 = third typed-area cell ($ a b)
  }));
  expect(predicted.text).toContain("ab");
  expect(predicted.bDim).toBe(true); // unconfirmed → dim

  // After the round-trip echo reconciles, the same cell is no longer dim.
  await page.waitForFunction(() => window.__ta.cellIsDim(3, 0) === false, undefined, {
    timeout: rtt + 1000,
  });
  const confirmed = await page.evaluate(() => window.__ta.rowText(0));
  expect(confirmed).toContain("ab");
});

test("captures a screenshot of an in-flight (dimmed) prediction", async ({ page }) => {
  const rtt = await page.evaluate(() => window.__ta.rttMs);
  // Prime + confirm the first char.
  await page.evaluate(() => window.__ta.type("l"));
  await page.waitForTimeout(rtt + 100);
  // Type a burst; with RTT=250ms these are all unconfirmed (dim) at capture time.
  await page.evaluate(() => window.__ta.type("s -la"));
  // Capture while predictions are still in flight (before the echo returns).
  await page.screenshot({
    path: "src/components/blocks/typeahead/__harness__/.prediction.png",
  });
  // Sanity: at least one of the burst cells is dim right now.
  const anyDim = await page.evaluate(
    () => window.__ta.cellIsDim(3, 0) || window.__ta.cellIsDim(4, 0) || window.__ta.cellIsDim(5, 0),
  );
  expect(anyDim).toBe(true);
});
