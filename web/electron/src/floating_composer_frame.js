"use strict";

/**
 * Where the floating New Agent composer opens. Pure geometry over Electron's
 * `Display.workArea` rectangles (top-left origin, DIPs).
 *
 * A remembered frame keeps its position as a fraction of the free travel on
 * its display, so moving it to another display of a different size lands in
 * the same relative spot instead of off-screen.
 */

const FLOATING_COMPOSER_SIZE = Object.freeze({
  default: Object.freeze({ width: 620, height: 360 }),
  min: Object.freeze({ width: 620, height: 300 }),
  max: Object.freeze({ width: 1000, height: 800 }),
});

/** @typedef {{ x: number, y: number, width: number, height: number }} Rect */
/** @typedef {{ id: number, workArea: Rect }} DisplayLike */

function isFiniteRect(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    ["x", "y", "width", "height"].every((key) => Number.isFinite(value[key])) &&
    value.width > 0 &&
    value.height > 0
  );
}

function clampNumber(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

/**
 * The size limits that apply on `workArea`: the composer's own min/max, but
 * never larger than the display itself.
 *
 * @param {Rect} workArea
 */
function sizeLimits(workArea) {
  const maxWidth = Math.min(FLOATING_COMPOSER_SIZE.max.width, workArea.width);
  const maxHeight = Math.min(FLOATING_COMPOSER_SIZE.max.height, workArea.height);
  return {
    minWidth: Math.min(FLOATING_COMPOSER_SIZE.min.width, maxWidth),
    minHeight: Math.min(FLOATING_COMPOSER_SIZE.min.height, maxHeight),
    maxWidth,
    maxHeight,
  };
}

/**
 * `frame` resized into the composer's limits and moved fully inside `workArea`.
 *
 * @param {Rect} frame
 * @param {Rect} workArea
 * @returns {Rect}
 */
function clampFrame(frame, workArea) {
  const limits = sizeLimits(workArea);
  const width = Math.round(clampNumber(frame.width, limits.minWidth, limits.maxWidth));
  const height = Math.round(clampNumber(frame.height, limits.minHeight, limits.maxHeight));
  const x = Math.round(clampNumber(frame.x, workArea.x, workArea.x + workArea.width - width));
  const y = Math.round(clampNumber(frame.y, workArea.y, workArea.y + workArea.height - height));
  return { x, y, width, height };
}

/**
 * The default-size composer centred in `workArea`.
 *
 * @param {Rect} workArea
 * @returns {Rect}
 */
function centeredFrame(workArea) {
  const { width, height } = FLOATING_COMPOSER_SIZE.default;
  return clampFrame(
    {
      x: workArea.x + (workArea.width - width) / 2,
      y: workArea.y + (workArea.height - height) / 2,
      width,
      height,
    },
    workArea,
  );
}

function intersectionArea(a, b) {
  const width = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const height = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return width > 0 && height > 0 ? width * height : 0;
}

/**
 * The display that shows most of `frame`, or null when it is on none of them
 * (a display was unplugged).
 *
 * @param {Rect} frame
 * @param {DisplayLike[]} displays
 * @returns {DisplayLike | null}
 */
function displayShowingFrame(frame, displays) {
  let best = null;
  let bestArea = 0;
  for (const display of displays) {
    const area = intersectionArea(frame, display.workArea);
    if (area > bestArea) {
      best = display;
      bestArea = area;
    }
  }
  return best;
}

function travelFraction(position, origin, freeTravel) {
  if (freeTravel <= 0) return 0.5;
  return clampNumber((position - origin) / freeTravel, 0, 1);
}

/**
 * `frame` moved from `source` to `target`, keeping its position as a fraction
 * of the free travel on each axis and its size where the target allows it.
 *
 * @param {Rect} frame
 * @param {Rect} source
 * @param {Rect} target
 * @returns {Rect}
 */
function translateFrame(frame, source, target) {
  const sized = clampFrame({ ...frame, x: target.x, y: target.y }, target);
  const fractionX = travelFraction(frame.x, source.x, source.width - frame.width);
  const fractionY = travelFraction(frame.y, source.y, source.height - frame.height);
  return clampFrame(
    {
      x: target.x + fractionX * (target.width - sized.width),
      y: target.y + fractionY * (target.height - sized.height),
      width: sized.width,
      height: sized.height,
    },
    target,
  );
}

/**
 * The frame to open the composer at, on the display under the pointer. A remembered frame on
 * that display keeps its place; one from another display is carried over proportionally; one
 * whose display is gone keeps its size, centred; and without one the composer opens centred.
 *
 * @param {{ savedFrame?: unknown, displays: DisplayLike[], pointerDisplay: DisplayLike }} options
 * @returns {Rect}
 */
function placeFloatingComposer({ savedFrame, displays, pointerDisplay }) {
  const target = pointerDisplay.workArea;
  const saved = isFiniteRect(savedFrame) ? savedFrame : null;
  if (saved === null) return centeredFrame(target);
  const savedDisplay = displayShowingFrame(saved, displays);
  if (savedDisplay === null) {
    // Its display is gone, so where it sat says nothing here: keep its size, centred.
    const { width, height } = clampFrame({ ...saved, x: target.x, y: target.y }, target);
    const x = target.x + (target.width - width) / 2;
    return clampFrame({ x, y: target.y + (target.height - height) / 2, width, height }, target);
  }
  if (savedDisplay.id === pointerDisplay.id) return clampFrame(saved, target);
  return translateFrame(saved, savedDisplay.workArea, target);
}

module.exports = {
  FLOATING_COMPOSER_SIZE,
  centeredFrame,
  clampFrame,
  displayShowingFrame,
  isFiniteRect,
  placeFloatingComposer,
  translateFrame,
};
