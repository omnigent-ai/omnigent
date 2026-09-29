const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const { JSDOM, VirtualConsole } = require("jsdom");

const { buildDesignModeScript } = require("../src/designModeScript");

const NONCE = "dialog-picker-test";
const FIELDS = `<label for="period">Period</label><input id="period" value="W41">
  <button id="save" type="submit">Save</button>`;
const MODAL_FORM = `<form id="modal" role="dialog"
  style="transform:translate(-50%, -50%);overflow:hidden;pointer-events:auto">${FIELDS}</form>`;
const GLOBAL_EVENTS = new Set([
  "mousemove",
  "click",
  "scroll",
  "focusin",
  "transitionend",
  "animationend",
  "resize",
  "keydown",
]);

// JSDOM has no layout: these are explicit inputs to positioning and visibility checks.
function setRect(element, { x = 200, y = 100, width = 120, height = 30 } = {}) {
  const rect = { x, y, width, height, left: x, top: y, right: x + width, bottom: y + height };
  element.getBoundingClientRect = () => rect;
  element.getClientRects = () => [rect];
}

function createPicker(t, html = MODAL_FORM, beforeEnable = () => {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error));
  const { window } = new JSDOM(html, {
    url: "https://picker.test/",
    runScripts: "outside-only",
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { document } = window;
  t.after(() => {
    window["__omniDisableDesignMode"]?.();
    window.close();
    assert.deepEqual(errors, [], "event handlers must not throw inside JSDOM");
  });
  t.mock.timers.enable({ apis: ["setTimeout"] });
  window.setTimeout = setTimeout;
  window.clearTimeout = clearTimeout;
  const timeouts = {
    scheduled: t.mock.method(window, "setTimeout"),
    canceled: t.mock.method(window, "clearTimeout"),
  };

  // Stub only popover API state; this does not simulate top-layer layout or focus.
  const openPopovers = new Set();
  const popoverShows = [];
  const matches = window.Element.prototype.matches;
  window.Element.prototype.matches = function (selector) {
    if (selector !== ":popover-open") return matches.call(this, selector);
    if (!this.isConnected) openPopovers.delete(this);
    return openPopovers.has(this);
  };
  window.HTMLElement.prototype.showPopover = function () {
    openPopovers.add(this);
    popoverShows.push({ element: this, host: this.parentElement });
  };
  window.HTMLElement.prototype.hidePopover = function () {
    openPopovers.delete(this);
  };

  const get = (id) => document.getElementById(id);
  for (const element of document.querySelectorAll("input, button")) setRect(element);
  beforeEnable(window);
  const focus = t.mock.method(window.HTMLElement.prototype, "focus");
  const listeners = [document, window].map((target) => ({
    added: t.mock.method(target, "addEventListener"),
    removed: t.mock.method(target, "removeEventListener"),
  }));
  const observed = t.mock.method(window.MutationObserver.prototype, "observe");
  const disconnected = t.mock.method(window.MutationObserver.prototype, "disconnect");
  const messages = [];
  window.console.log = (message) => messages.push(String(message));
  let hit = null;
  document.elementFromPoint = () => hit;
  const script = buildDesignModeScript(NONCE);
  window.eval(script);
  const popup = get("__omni-popup");
  Object.defineProperties(popup, {
    offsetWidth: { value: 300 },
    offsetHeight: { value: 110 },
  });

  function mouse(target, type) {
    hit = target;
    const rect = target.getBoundingClientRect();
    const event = new window.MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      button: 0,
      clientX: rect.left + 1,
      clientY: rect.top + 1,
    });
    target.dispatchEvent(event);
    return event;
  }
  return {
    window,
    document,
    get,
    popup,
    input: get("__omni-popup-input"),
    layer: get("__omni-design-layer"),
    focus,
    listeners,
    observed,
    disconnected,
    messages,
    script,
    popoverShows,
    timeouts,
    clock: t.mock.timers,
    mouse,
    select(target = get("period")) {
      for (const type of ["mousemove", "mousedown", "mouseup"]) mouse(target, type);
      return mouse(target, "click");
    },
    key(target, key, options = {}) {
      const event = new window.KeyboardEvent("keydown", {
        key,
        bubbles: true,
        cancelable: true,
        ...options,
      });
      target.dispatchEvent(event);
      return event;
    },
    payloads(kind) {
      const prefix = `__omni_${NONCE}_element_${kind}__`;
      return messages
        .filter((message) => message.startsWith(prefix))
        .map((message) => JSON.parse(message.slice(prefix.length)));
    },
    dismissals: () => messages.filter((message) => message === `__omni_${NONCE}_element_dismiss__`),
  };
}

describe("dialog-hosted design popup", () => {
  for (const { name, html, host } of [
    { name: "body fallback", html: `<form>${FIELDS}</form>`, host: "body" },
    {
      name: "an open native dialog",
      html: `<dialog id="modal" open><form>${FIELDS}</form></dialog>`,
      host: "#modal",
    },
    {
      name: "the nearest ARIA dialog",
      html: `<dialog open>${MODAL_FORM}</dialog>`,
      host: "#modal",
    },
    {
      name: "the nearest alertdialog",
      html: `<section role="dialog"><form id="modal" role="alertdialog">${FIELDS}</form></section>`,
      host: "#modal",
    },
  ]) {
    it(`mounts highlights and the floating popup in ${name}, not the selected input`, (t) => {
      const picker = createPicker(t, html);
      const event = picker.select();
      const { document, layer, popup } = picker;
      assert.equal(event.defaultPrevented, true);
      assert.equal(layer?.parentElement, document.querySelector(host));
      assert.equal(picker.get("period").childElementCount, 0);
      for (const id of ["__omni-highlight", "__omni-label", "__omni-popup"]) {
        assert.equal(picker.get(id).parentElement, layer);
      }
      assert.equal(layer.getAttribute("popover"), "manual");
      assert.equal(layer.style.pointerEvents, "none");
      assert.equal(popup.style.pointerEvents, "auto");
      assert.deepEqual(picker.popoverShows.at(-1), {
        element: layer,
        host: document.querySelector(host),
      });
      assert.equal(picker.payloads("select")[0].id, "#period");
    });
  }

  it("keeps prompt typing inside the modal focus scope and leaves the page field at W41", (t) => {
    let pageInputs = 0;
    const picker = createPicker(t, MODAL_FORM, ({ document }) => {
      const modal = document.getElementById("modal");
      const period = document.getElementById("period");
      document.body.style.pointerEvents = "none";
      period.focus();
      document.addEventListener("focusin", (event) => {
        if (!modal.contains(event.target)) period.focus();
      });
      modal.addEventListener("input", () => pageInputs++);
    });
    picker.select();
    picker.clock.tick(30);
    assert.equal(picker.document.activeElement, picker.input);
    picker.document.activeElement.value = "Make the label clearer";
    picker.input.dispatchEvent(new picker.window.InputEvent("input", { bubbles: true }));
    assert.equal(picker.get("period").value, "W41");
    assert.equal(pageInputs, 0);

    picker.mouse(picker.get("__omni-popup-close"), "click");
    assert.equal(picker.popup.style.display, "none");
    assert.equal(picker.document.activeElement, picker.get("period"));
    assert.equal(picker.dismissals().length, 1);
    assert.equal(picker.payloads("select").length, 1);
  });

  for (const action of ["Enter", "Send click"]) {
    it(`${action} submits the prompt once without submitting the surrounding form`, (t) => {
      let formSubmits = 0;
      let pageEnters = 0;
      const picker = createPicker(t, MODAL_FORM, ({ document }) => {
        const modal = document.getElementById("modal");
        modal.addEventListener("submit", (event) => {
          event.preventDefault();
          formSubmits++;
        });
        document.addEventListener(
          "keydown",
          (event) => {
            if (event.key === "Enter") {
              pageEnters++;
              modal.requestSubmit();
            }
          },
          true,
        );
      });
      picker.select();
      picker.clock.tick(30);
      assert.equal(picker.input.form, picker.get("modal"));
      picker.input.value = "  Make the label clearer  ";
      if (action === "Enter") {
        // JSDOM has no implicit Enter submission; cancellation is checked explicitly.
        assert.equal(picker.key(picker.input, "Enter").defaultPrevented, true);
        picker.key(picker.input, "Enter", { repeat: true });
      } else {
        picker.mouse(picker.get("__omni-popup-send"), "click");
      }
      picker.get("__omni-popup-send").click();
      const submissions = picker.payloads("prompt_submit");
      assert.equal(submissions.length, 1);
      assert.equal(submissions[0].prompt, "Make the label clearer");
      assert.equal(submissions[0].element.id, "#period");
      assert.equal(formSubmits, 0);
      assert.equal(pageEnters, 0);
      assert.equal(picker.get("period").value, "W41");
    });
  }

  it("cancels Shift+Enter without submitting either the prompt or the surrounding form", (t) => {
    const picker = createPicker(t);
    let pageEnters = 0;
    picker.document.addEventListener(
      "keydown",
      (event) => {
        if (event.key === "Enter") pageEnters++;
      },
      true,
    );
    picker.select();
    picker.input.value = "Keep this draft";
    assert.equal(picker.input.form, picker.get("modal"));
    assert.equal(picker.key(picker.input, "Enter", { shiftKey: true }).defaultPrevented, true);
    assert.equal(pageEnters, 0);
    assert.deepEqual(picker.payloads("prompt_submit"), []);
    assert.equal(picker.input.value, "Keep this draft");
    assert.equal(picker.input.disabled, false);
  });

  it("cancels held Enter after submission moves focus to Close", (t) => {
    const picker = createPicker(t);
    picker.select();
    picker.clock.tick(30);
    picker.input.value = "Keep the form unchanged";
    picker.key(picker.document.activeElement, "Enter");
    assert.equal(picker.document.activeElement, picker.get("__omni-popup-close"));
    const repeat = picker.key(picker.document.activeElement, "Enter", { repeat: true });
    assert.equal(repeat.defaultPrevented, true);
    assert.equal(picker.payloads("prompt_submit").length, 1);
    assert.equal(picker.popup.style.display, "block");
    assert.equal(picker.get("period").value, "W41");
  });

  it("lets Tab and Shift+Tab reach the parent modal's focus scope", (t) => {
    const tabs = [];
    const picker = createPicker(t, MODAL_FORM, ({ document }) => {
      document.getElementById("modal").addEventListener("keydown", (event) => {
        tabs.push({ key: event.key, shiftKey: event.shiftKey });
        event.preventDefault();
      });
    });
    picker.select();
    for (const shiftKey of [false, true]) {
      assert.equal(picker.key(picker.input, "Tab", { shiftKey }).defaultPrevented, true);
    }
    picker.key(picker.input, "a");
    assert.deepEqual(tabs, [
      { key: "Tab", shiftKey: false },
      { key: "Tab", shiftKey: true },
    ]);
  });

  it("handles Escape on popup controls before an existing document-capture modal handler", (t) => {
    let modalEscapes = 0;
    const picker = createPicker(
      t,
      `<dialog id="modal" open><form>${FIELDS}</form></dialog>`,
      ({ document }) => {
        document.addEventListener(
          "keydown",
          (event) => {
            if (event.key === "Escape") {
              modalEscapes++;
              document.getElementById("modal").removeAttribute("open");
            }
          },
          true,
        );
      },
    );
    for (const id of ["__omni-popup-input", "__omni-popup-close", "__omni-popup-send"]) {
      picker.select();
      picker.clock.tick(30);
      picker.get(id).focus();
      assert.equal(picker.key(picker.get(id), "Escape").defaultPrevented, true);
      assert.equal(picker.popup.style.display, "none");
      assert.equal(picker.get("modal").open, true);
    }
    assert.equal(modalEscapes, 0);
    assert.equal(picker.dismissals().length, 3);
    picker.key(picker.get("period"), "Escape");
    assert.equal(modalEscapes, 1, "page Escape must still work outside the popup");
  });

  it("keeps Escape inside the popup while a submission is pending", (t) => {
    let modalEscapes = 0;
    const picker = createPicker(
      t,
      `<dialog id="modal" open><form>${FIELDS}</form></dialog>`,
      ({ document }) => {
        document.getElementById("period").focus();
        document.addEventListener(
          "keydown",
          (event) => {
            if (event.key === "Escape") {
              modalEscapes++;
              document.getElementById("modal").removeAttribute("open");
            }
          },
          true,
        );
      },
    );
    picker.select();
    picker.clock.tick(30);
    const close = picker.get("__omni-popup-close");
    const send = picker.get("__omni-popup-send");
    let disabledAtCloseFocus;
    close.addEventListener("focus", () => {
      disabledAtCloseFocus = [picker.input.disabled, send.disabled];
    });
    picker.input.value = "Make the label clearer";
    picker.key(picker.document.activeElement, "Enter");
    assert.deepEqual(disabledAtCloseFocus, [false, false]);
    assert.equal(picker.document.activeElement, close);
    assert.deepEqual([picker.input.disabled, send.disabled], [true, true]);
    assert.equal(picker.payloads("prompt_submit").length, 1);

    assert.equal(picker.key(picker.document.activeElement, "Escape").defaultPrevented, true);
    assert.equal(picker.popup.style.display, "none");
    assert.equal(picker.layer.matches(":popover-open"), false);
    assert.equal(picker.get("modal").open, true);
    assert.equal(modalEscapes, 0);
    assert.equal(picker.dismissals().length, 1);
  });

  for (const action of ["close", "Escape", "disable"]) {
    it(`cancels delayed popup focus on ${action}`, (t) => {
      const picker = createPicker(t, MODAL_FORM, ({ document }) => {
        document.getElementById("period").focus();
      });
      picker.select();
      const focusTimer = picker.timeouts.scheduled.mock.calls.at(-1).result;
      if (action === "disable") picker.window["__omniDisableDesignMode"]();
      else if (action === "Escape") picker.key(picker.input, "Escape");
      else picker.mouse(picker.get("__omni-popup-close"), "click");
      assert.ok(
        picker.timeouts.canceled.mock.calls.some((call) => call.arguments[0] === focusTimer),
        "the pending focus timer must be canceled",
      );
      picker.clock.tick(100);
      assert.equal(
        picker.focus.mock.calls.filter((call) => call.this === picker.input).length,
        0,
        "a hidden or detached popup must not receive a stale focus call",
      );
      assert.equal(picker.document.activeElement, picker.get("period"));
      assert.equal(picker.dismissals().length, action === "disable" ? 0 : 1);
    });
  }

  it("re-anchors the selected highlight and popup on nested scroll and window resize", (t) => {
    const picker = createPicker(t, `${MODAL_FORM}<button id="other">Other</button>`);
    const target = picker.get("period");
    const highlight = picker.get("__omni-highlight");
    picker.select();
    picker.mouse(picker.get("other"), "mousemove");
    setRect(target, { x: 300, y: 180, width: 200, height: 40 });
    picker.get("modal").dispatchEvent(new picker.window.Event("scroll"));
    assert.deepEqual(
      [highlight.style.left, highlight.style.top, highlight.style.width, highlight.style.height],
      ["300px", "180px", "200px", "40px"],
    );
    assert.deepEqual([picker.popup.style.left, picker.popup.style.top], ["250px", "228px"]);
    assert.equal(picker.get("__omni-label").style.top, "158px");

    setRect(target, { x: 260, y: 240, width: 160, height: 32 });
    picker.window.dispatchEvent(new picker.window.Event("resize"));
    assert.deepEqual([highlight.style.left, highlight.style.top], ["260px", "240px"]);
    assert.deepEqual([picker.popup.style.left, picker.popup.style.top], ["190px", "280px"]);
    assert.equal(picker.payloads("select").length, 1);
  });

  it("closes the hover-only popover when its dialog becomes inert", async (t) => {
    const picker = createPicker(t);
    picker.mouse(picker.get("period"), "mousemove");
    assert.equal(picker.layer.matches(":popover-open"), true);
    assert.equal(picker.popup.style.display, "none");
    await new Promise(setImmediate);
    picker.get("modal").setAttribute("inert", "");
    await new Promise(setImmediate);
    assert.equal(picker.get("period").getClientRects().length, 1);
    assert.equal(picker.layer.matches(":popover-open"), false);
    assert.equal(picker.get("__omni-highlight").style.display, "none");
    assert.equal(picker.get("__omni-label").style.display, "none");
    assert.deepEqual(picker.messages, []);

    picker.get("modal").removeAttribute("inert");
    picker.window.dispatchEvent(new picker.window.Event("resize"));
    assert.equal(picker.layer.matches(":popover-open"), false);
    picker.mouse(picker.get("period"), "mousemove");
    assert.equal(picker.layer.matches(":popover-open"), true);
  });

  it("dismisses a visibility-hidden target even when its client rect remains nonempty", async (t) => {
    const picker = createPicker(t);
    let visible = true;
    const checkVisibility = t.mock.fn(() => visible);
    picker.get("period").checkVisibility = checkVisibility;
    picker.select();
    picker.clock.tick(30);
    await new Promise(setImmediate);
    visible = false;
    picker.get("modal").style.visibility = "hidden";
    await new Promise(setImmediate);
    assert.equal(picker.get("period").getClientRects().length, 1);
    assert.equal(checkVisibility.mock.calls.at(-1).arguments[0].visibilityProperty, true);
    assert.equal(picker.popup.style.display, "none");
    assert.equal(picker.layer.matches(":popover-open"), false);
    assert.equal(picker.dismissals().length, 1);
  });

  for (const type of ["transitionend", "animationend"]) {
    it(`dismisses after ${type} when visibility changes without a DOM mutation`, async (t) => {
      const picker = createPicker(t);
      let visible = true;
      picker.get("period").checkVisibility = () => visible;
      picker.select();
      picker.clock.tick(30);
      await new Promise(setImmediate);
      assert.equal(picker.popup.style.display, "block");

      visible = false;
      picker.get("modal").dispatchEvent(new picker.window.Event(type, { bubbles: true }));
      assert.equal(picker.popup.style.display, "none");
      assert.equal(picker.layer.matches(":popover-open"), false);
    });
  }

  it("allows selecting a visible decorative aria-hidden icon inside a dialog", async (t) => {
    const picker = createPicker(
      t,
      `<form id="modal" role="dialog">${FIELDS}
        <button type="button"><span id="icon" aria-hidden="true">★</span></button></form>`,
    );
    const icon = picker.get("icon");
    setRect(icon);
    icon.checkVisibility = () => true;
    picker.select(icon);
    picker.clock.tick(30);
    await new Promise(setImmediate);
    assert.equal(picker.popup.style.display, "block");
    assert.equal(picker.layer.matches(":popover-open"), true);
    assert.equal(picker.document.activeElement, picker.input);
    assert.equal(picker.payloads("select")[0].id, "#icon");
    assert.equal(picker.dismissals().length, 0);
  });

  it("dismisses when another dialog takes focus without stealing that focus back", (t) => {
    const picker = createPicker(
      t,
      `${MODAL_FORM}<section id="other-modal" role="dialog"><input id="other"></section>`,
      ({ document }) => document.getElementById("period").focus(),
    );
    picker.select();
    picker.clock.tick(30);
    picker.get("save").focus();
    assert.equal(picker.popup.style.display, "block", "focus within the same dialog is allowed");
    picker.input.focus();
    picker.get("other").focus();
    picker.clock.tick(100);
    assert.equal(picker.document.activeElement, picker.get("other"));
    assert.equal(picker.popup.style.display, "none");
    assert.equal(picker.layer.matches(":popover-open"), false);
    assert.equal(picker.dismissals().length, 1);
    assert.equal(picker.get("period").value, "W41");
    picker.select(picker.get("other"));
    assert.equal(picker.layer.parentElement, picker.get("other-modal"));
    assert.equal(picker.popup.style.display, "block");
  });

  for (const [action, dismissModal] of [
    ["removed", (modal) => modal.remove()],
    ["closed", (modal) => modal.removeAttribute("open")],
    ["hidden", (modal) => (modal.hidden = true)],
    ["style-hidden", (modal) => (modal.style.display = "none")],
    ["class-hidden", (modal) => modal.classList.add("closed")],
    ["aria-hidden", (modal) => modal.setAttribute("aria-hidden", "true")],
    ["data-state-closed", (modal) => modal.setAttribute("data-state", "closed")],
  ]) {
    it(`dismisses a ${action} modal's popup and reattaches on the next selection`, async (t) => {
      const picker = createPicker(
        t,
        `<dialog id="modal" open><form>${FIELDS}</form></dialog>
          <button id="outside">Outside</button>
          <section id="next-modal" role="dialog"><button id="next">Next</button></section>`,
      );
      picker.select();
      const highlight = picker.get("__omni-highlight");
      // Drain mounting mutations so only dismissal can notify the observer below.
      await new Promise(setImmediate);
      picker.get("period").getClientRects = () => [];
      dismissModal(picker.get("modal"));
      await new Promise(setImmediate);
      assert.equal(picker.popup.style.display, "none");
      assert.equal(highlight.style.display, "none");
      assert.equal(picker.dismissals().length, 1);
      picker.clock.tick(100);
      assert.equal(picker.focus.mock.calls.filter((call) => call.this === picker.input).length, 0);

      picker.select(picker.get("outside"));
      assert.equal(picker.layer.parentElement, picker.document.body);
      assert.equal(picker.popup.style.display, "block");
      picker.select(picker.get("next"));
      assert.equal(picker.layer.parentElement, picker.get("next-modal"));
      assert.equal(picker.payloads("select").at(-1).id, "#next");
    });
  }

  it("re-enables with a fresh layer and nonce without keeping the previous handlers or focus timer", (t) => {
    const picker = createPicker(t);
    picker.select();
    const oldObservers = picker.observed.mock.calls.map((call) => call.this);
    const newNonce = "replacement-picker";
    picker.window.eval(buildDesignModeScript(newNonce));
    assert.equal(picker.layer.isConnected, false);
    assert.equal(picker.popup.isConnected, false);
    assert.equal(picker.document.querySelectorAll("#__omni-design-layer").length, 1);
    assert.equal(picker.document.querySelectorAll("style").length, 1);
    for (const observer of oldObservers) {
      assert.ok(picker.disconnected.mock.calls.some((call) => call.this === observer));
    }
    picker.messages.length = 0;
    picker.select();
    picker.clock.tick(30);
    const input = picker.get("__omni-popup-input");
    assert.equal(picker.document.activeElement, input);
    assert.equal(picker.focus.mock.calls.filter((call) => call.this === picker.input).length, 0);
    input.value = "Use the new nonce";
    picker.key(input, "Enter");
    picker.key(input, "Escape");
    assert.deepEqual(
      picker.messages.map((message) => message.split("{", 1)[0]),
      [
        `__omni_${newNonce}_element_select__`,
        `__omni_${newNonce}_element_prompt_submit__`,
        `__omni_${newNonce}_element_dismiss__`,
      ],
    );
  });

  it("tears down timers, observers, global listeners, layer, and style", async (t) => {
    const picker = createPicker(
      t,
      `<style id="page-style">form { color: black; }</style>${MODAL_FORM}`,
    );
    assert.equal(picker.document.querySelectorAll("style").length, 2);
    picker.select();
    picker.input.value = "Make the label clearer";
    picker.key(picker.input, "Enter");
    const messages = [...picker.messages];
    picker.window["__omniDisableDesignMode"]();

    for (const call of picker.timeouts.scheduled.mock.calls) {
      assert.ok(
        picker.timeouts.canceled.mock.calls.some((entry) => entry.arguments[0] === call.result),
      );
    }
    const capture = (options) => (typeof options === "boolean" ? options : !!options?.capture);
    for (const { added, removed } of picker.listeners) {
      for (const call of added.mock.calls.filter((entry) =>
        GLOBAL_EVENTS.has(entry.arguments[0]),
      )) {
        const [type, listener, options] = call.arguments;
        assert.ok(
          removed.mock.calls.some(
            ({ arguments: [removedType, removedListener, removedOptions] }) =>
              type === removedType &&
              listener === removedListener &&
              capture(options) === capture(removedOptions),
          ),
          `${type} listener must be removed with its original capture flag`,
        );
      }
    }
    assert.ok(picker.observed.mock.calls.length > 0);
    for (const call of picker.observed.mock.calls) {
      assert.ok(picker.disconnected.mock.calls.some((entry) => entry.this === call.this));
    }
    assert.equal(picker.layer.isConnected, false);
    assert.equal(picker.popup.isConnected, false);
    assert.deepEqual([...picker.document.querySelectorAll("style")], [picker.get("page-style")]);
    for (const key of [
      "__omniDesignMode",
      "__omniDisableDesignMode",
      "__omniOnDesignResult",
      "__omniSelectedEl",
    ]) {
      assert.equal(picker.window[key], undefined);
    }
    picker.clock.tick(10000);
    picker.select();
    picker.window.dispatchEvent(new picker.window.Event("resize"));
    await new Promise(setImmediate);
    assert.deepEqual(picker.messages, messages);
    assert.equal(picker.focus.mock.calls.filter((call) => call.this === picker.input).length, 0);

    picker.window.eval(picker.script);
    picker.select();
    assert.equal(picker.get("__omni-design-layer").isConnected, true);
    assert.equal(picker.payloads("select").length, 2);
  });
});
