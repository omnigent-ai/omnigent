"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  FLOATING_COMPOSER_SIZE,
  centeredFrame,
  clampFrame,
  displayShowingFrame,
  placeFloatingComposer,
  translateFrame,
} = require("../src/floating_composer_frame");

const laptop = { id: 1, workArea: { x: 0, y: 25, width: 1512, height: 920 } };
const external = { id: 2, workArea: { x: 1512, y: 0, width: 2560, height: 1415 } };
const small = { id: 3, workArea: { x: -800, y: 0, width: 800, height: 560 } };
const displays = [laptop, external, small];

test("opens centred at the default size without a remembered frame", () => {
  assert.deepEqual(centeredFrame(laptop.workArea), { x: 446, y: 305, width: 620, height: 360 });
  assert.deepEqual(placeFloatingComposer({ displays, pointerDisplay: external }), {
    x: 2482,
    y: 528,
    width: 620,
    height: 360,
  });
});

test("clamps size to the composer limits and position into the work area", () => {
  assert.deepEqual(clampFrame({ x: -50, y: 900, width: 2000, height: 100 }, laptop.workArea), {
    x: 0,
    y: 645,
    width: FLOATING_COMPOSER_SIZE.max.width,
    height: FLOATING_COMPOSER_SIZE.min.height,
  });
});

test("never exceeds a display smaller than the composer's minimum", () => {
  const tiny = { x: 0, y: 0, width: 500, height: 250 };
  assert.deepEqual(clampFrame({ x: 10, y: 10, width: 620, height: 360 }, tiny), {
    x: 0,
    y: 0,
    width: 500,
    height: 250,
  });
});

test("finds the display showing most of a frame, or none", () => {
  const straddling = { x: 1300, y: 100, width: 620, height: 360 };
  assert.equal(displayShowingFrame(straddling, displays)?.id, external.id);
  assert.equal(displayShowingFrame({ x: 9000, y: 9000, width: 620, height: 360 }, displays), null);
});

test("moves a frame to another display by its fraction of free travel", () => {
  // Bottom-right corner of the laptop stays bottom-right on the external display.
  const corner = { x: 892, y: 585, width: 620, height: 360 };
  assert.deepEqual(translateFrame(corner, laptop.workArea, external.workArea), {
    x: 3452,
    y: 1055,
    width: 620,
    height: 360,
  });
  const centre = centeredFrame(laptop.workArea);
  assert.deepEqual(
    translateFrame(centre, laptop.workArea, external.workArea),
    centeredFrame(external.workArea),
  );
});

test("the shortcut follows the pointer, carrying the remembered frame across", () => {
  const savedFrame = { x: 0, y: 25, width: 700, height: 400 };
  assert.deepEqual(
    placeFloatingComposer({
      savedFrame,
      displays,
      pointerDisplay: external,
    }),
    { x: 1512, y: 0, width: 700, height: 400 },
  );
});

test("reopening on the same display keeps the remembered frame", () => {
  const savedFrame = { x: 2000, y: 200, width: 800, height: 500 };
  assert.deepEqual(
    placeFloatingComposer({ savedFrame, displays, pointerDisplay: external }),
    savedFrame,
  );
});

test("a remembered frame shrinks to fit a smaller display", () => {
  const savedFrame = { x: 1600, y: 100, width: 1000, height: 800 };
  const placed = placeFloatingComposer({
    savedFrame,
    displays,
    pointerDisplay: small,
  });
  assert.deepEqual(placed, { x: -800, y: 0, width: 800, height: 560 });
});

test("a frame left on an unplugged display keeps its size, centred on the pointer's display", () => {
  for (const savedFrame of [
    { x: 9000, y: 9000, width: 700, height: 400 },
    { x: -9000, y: -9000, width: 700, height: 400 },
  ]) {
    const placed = placeFloatingComposer({
      savedFrame,
      displays: [laptop],
      pointerDisplay: laptop,
    });
    assert.deepEqual(placed, { x: 406, y: 285, width: 700, height: 400 });
    assert.equal(displayShowingFrame(placed, [laptop])?.id, laptop.id);
  }
});

test("ignores a malformed remembered frame", () => {
  for (const savedFrame of [null, "x", { x: 1 }, { x: 0, y: 0, width: Number.NaN, height: 1 }]) {
    assert.deepEqual(
      placeFloatingComposer({ savedFrame, displays, pointerDisplay: laptop }),
      centeredFrame(laptop.workArea),
    );
  }
});
