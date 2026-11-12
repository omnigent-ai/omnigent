"use strict";

// browser_scroll's mouse wheel (src/browserInput.js and
// the scroll scripts in src/agentActionScripts.mjs). A wheel on a
// container with no room left scrolls whatever is behind it, so the tool must
// not send one, and must say so if the wheel moved something else.

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const { JSDOM } = require("jsdom");
const { fileChooserGuard, performBrowserInput } = require("../src/browserInput");
const { buildAgentActionScript } = require("../src/agentActionScripts.mjs");
const { makeEntry, mouseEvents, runInPage } = require("./browserInputFakes");

fileChooserGuard.ms = 0;

describe("performBrowserInput: scroll", () => {
  it("scrolls with a mouse wheel and reports where it settled", async () => {
    const positions = [100, 400, 640, 640, 640];
    const { entry, commands } = makeEntry({
      answer: (step) => {
        if (step === "scroll_target")
          return JSON.stringify({
            x: 50,
            y: 60,
            delta: 540,
            vertical: true,
            room: true,
            pos: 100,
            max: 640,
          });
        if (step === "scroll_state") {
          const pos = positions.length > 1 ? positions.shift() : positions[0];
          return JSON.stringify({ scroll_x: 0, scroll_y: pos, pos, max: 640, container: "page" });
        }
        return undefined;
      },
    });
    const result = await performBrowserInput(entry, "scroll", { direction: "down" });
    assert.deepEqual(result, {
      ok: true,
      scroll_x: 0,
      scroll_y: 640,
      at_end: true,
      container: "page",
    });
    const wheel = commands.find(([, p]) => p?.type === "mouseWheel")[1];
    assert.deepEqual([wheel.x, wheel.y, wheel.deltaX, wheel.deltaY], [50, 60, 0, 540]);
    assert.equal(
      commands.some(([, params]) => params?.type === "mouseMoved"),
      false,
      "scrolling does not trigger a hover that could change the measured container",
    );
  });

  it("scrolls the container directly when no visible spot reaches it", async () => {
    const scrolled = { scroll_x: 0, scroll_y: 300, at_end: false, container: "<div#list>" };
    const { entry, commands } = makeEntry({
      answer: (step) => {
        if (step === "scroll_target")
          return JSON.stringify({
            x: null,
            y: null,
            delta: 300,
            vertical: true,
            room: true,
            pos: 0,
            max: 900,
          });
        return step === "scroll" ? JSON.stringify(scrolled) : undefined;
      },
    });
    const result = await performBrowserInput(entry, "scroll", {
      selector: "#list",
      direction: "down",
      amount: 300,
    });
    assert.deepEqual(result, { ok: true, ...scrolled });
    assert.equal(mouseEvents(commands).length, 0);
  });

  it("sends no wheel to a container already at its end", async () => {
    const { entry, commands, steps } = makeEntry({
      answer: (step) =>
        step === "scroll_target"
          ? JSON.stringify({
              x: 50,
              y: 60,
              delta: 300,
              vertical: true,
              room: false,
              pos: 600,
              max: 600,
              scroll_x: 0,
              scroll_y: 600,
              container: "<div#list>",
            })
          : undefined,
    });
    const result = await performBrowserInput(entry, "scroll", {
      selector: "#list",
      direction: "down",
    });
    assert.deepEqual(result, {
      ok: true,
      scroll_x: 0,
      scroll_y: 600,
      at_end: true,
      container: "<div#list>",
    });
    assert.equal(mouseEvents(commands).length, 0, "no wheel reaches the page behind the list");
    assert.ok(!steps.includes("scroll"), "and nothing is scrolled directly either");
  });

  it("fails when the wheel scrolled something behind the container", async () => {
    const { entry } = makeEntry({
      answer: (step) => {
        if (step === "scroll_target")
          return JSON.stringify({
            x: 50,
            y: 60,
            delta: 300,
            vertical: true,
            room: true,
            pos: 500,
            max: 600,
          });
        if (step === "scroll_state")
          return JSON.stringify({
            scroll_x: 0,
            scroll_y: 600,
            pos: 600,
            max: 600,
            container: "<div#list>",
            outer_moved: "the page",
          });
        return undefined;
      },
    });
    await assert.rejects(
      performBrowserInput(entry, "scroll", { selector: "#list", direction: "down" }),
      /the wheel scrolled the page, not just <div#list>; take a snapshot/,
    );
  });
});

/** A page with a 400px list inside a 1000px-tall page, both scrollable. */
function nestedList({ listTop = 0, pageTop = 0 } = {}) {
  const dom = new JSDOM('<div id="list" style="overflow-y: auto"><div id="item">row</div></div>', {
    url: "https://example.com",
    runScripts: "outside-only",
  });
  const { window } = dom;
  const doc = window.document;
  const list = doc.querySelector("#list");
  const page = doc.documentElement;
  const tops = { list: listTop, page: pageTop };
  const box = (el, key, height, client) =>
    Object.defineProperties(el, {
      scrollHeight: { value: height },
      clientHeight: { value: client },
      clientWidth: { value: 300 },
      scrollTop: { get: () => tops[key] },
      scrollLeft: { get: () => 0 },
    });
  box(list, "list", 1000, 400);
  box(page, "page", 2000, 800);
  list.getBoundingClientRect = () => ({
    left: 0,
    top: 0,
    right: 300,
    bottom: 400,
    width: 300,
    height: 400,
  });
  Object.defineProperty(doc, "scrollingElement", { value: page });
  window.innerWidth = 1280;
  window.innerHeight = 800;
  doc.elementFromPoint = () => doc.querySelector("#item");
  const run = (step, args) => JSON.parse(runInPage(window, buildAgentActionScript(step, args)));
  return { dom, tops, run };
}

describe("scroll page scripts", () => {
  const args = { selector: "#list", direction: "down" };

  it("says a container at its end has no room that way, and one with room does", () => {
    const atEnd = nestedList({ listTop: 600 });
    try {
      assert.equal(atEnd.run("scroll_target", args).room, false);
      assert.equal(atEnd.run("scroll_target", { ...args, direction: "up" }).room, true);
    } finally {
      atEnd.dom.window.close();
    }
    const atTop = nestedList();
    try {
      assert.equal(atTop.run("scroll_target", args).room, true);
      assert.equal(atTop.run("scroll_target", { ...args, direction: "up" }).room, false);
    } finally {
      atTop.dom.window.close();
    }
  });

  it("reports the page when it moved instead of, or as well as, the container", () => {
    const { dom, tops, run } = nestedList({ listTop: 500 });
    try {
      run("scroll_target", args);
      tops.list = 600;
      assert.equal(run("scroll_state", args).outer_moved, null, "only the list moved");
      tops.page = 200;
      assert.equal(run("scroll_state", args).outer_moved, "the page");
    } finally {
      dom.window.close();
    }
  });
});

describe("right-to-left scrolling", () => {
  /** A 300px-wide right-to-left strip, 1000px of content, at `left`. */
  function rtlStrip(left) {
    const dom = new JSDOM(
      '<div id="strip" style="overflow-x: auto; direction: rtl"><div id="cell">cell</div></div>',
      { url: "https://example.com", runScripts: "outside-only" },
    );
    const { window } = dom;
    const strip = window.document.querySelector("#strip");
    Object.defineProperties(strip, {
      scrollWidth: { value: 1000 },
      clientWidth: { value: 300 },
      scrollHeight: { value: 40 },
      clientHeight: { value: 40 },
      scrollLeft: { get: () => left },
      scrollTop: { get: () => 0 },
    });
    strip.getBoundingClientRect = () => ({
      left: 0,
      top: 0,
      right: 300,
      bottom: 40,
      width: 300,
      height: 40,
    });
    window.innerWidth = 1280;
    window.innerHeight = 800;
    window.document.elementFromPoint = () => window.document.querySelector("#cell");
    const run = (direction) =>
      JSON.parse(
        runInPage(
          window,
          buildAgentActionScript("scroll_target", { selector: "#strip", direction }),
        ),
      );
    return { dom, run };
  }

  it("has room to the left from its right edge, and none past either end", () => {
    const start = rtlStrip(0);
    try {
      const left = start.run("left");
      assert.deepEqual([left.room, left.min, left.max], [true, -700, 0]);
      assert.equal(start.run("right").room, false);
    } finally {
      start.dom.window.close();
    }
    const end = rtlStrip(-700);
    try {
      assert.equal(end.run("left").room, false);
      assert.equal(end.run("right").room, true);
    } finally {
      end.dom.window.close();
    }
  });

  it("reports where right-to-left content ends, not 0", async () => {
    const positions = [-400, -400, -400];
    const target = -100;
    const { entry } = makeEntry({
      answer: (step) => {
        if (step === "scroll_target")
          return JSON.stringify({
            x: 50,
            y: 20,
            delta: -300,
            vertical: false,
            room: true,
            pos: target,
            min: -700,
            max: 0,
          });
        if (step === "scroll_state") {
          const pos = positions.length > 1 ? positions.shift() : positions[0];
          return JSON.stringify({
            scroll_x: pos,
            scroll_y: 0,
            pos,
            min: -700,
            max: 0,
            container: "<div#strip>",
            outer_moved: null,
          });
        }
        return undefined;
      },
    });
    const result = await performBrowserInput(entry, "scroll", {
      selector: "#strip",
      direction: "left",
    });
    assert.deepEqual([result.scroll_x, result.at_end], [-400, false], "300px more to the left");
  });
});
