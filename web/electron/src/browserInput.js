"use strict";

/**
 * Puppeteer supplies trusted keyboard/mouse input to the authorized Electron
 * view. Omnigent retains target, document, popup and deadline guards because
 * those protect the app's shared browser rather than implement input mechanics.
 */

const automationDriver = require("./browserAutomationDriver");
const { keyboardKey, parseKeyCombo } = require("./browserKeys");
const { buildAgentActionScript } = require("./agentActionScripts.mjs");
const { beforeAgentDeadline, DEFAULT_ACTION_MS, PAGE_TIMEOUT_ERROR } = require("./agentDeadline");

const CONTEXT_MENU_SUPPRESS_MS = 1000;
// A click handler can open a file picker a moment after the click.
const FILE_CHOOSER_SETTLE_MS = 150;
// Chromium's transient user activation (kActivationLifespan): a page may
// open a file picker this long after the agent's last trusted input, from a
// timer or a fetch, so pickers stay intercepted that long after the action.
// A test sets it to 0 to end interception with the action, as before.
const fileChooserGuard = { ms: 5000 };
// How long after an agent action detaches its input still counts as the
// agent's, for native input-event listeners that would otherwise take it as
// the user's (protocol input fires them too).
const AGENT_INPUT_GRACE_MS = 500;
const SCROLL_POLL_MS = 50;
// How long an action's cleanup releases may take, in all, before it lets go
// anyway; inside the 2 s the server waits beyond the relay's deadline.
const CLEANUP_WAIT_MS = 500;
const SCROLL_SETTLE_MAX_MS = 1500;
const SCROLL_START_WAIT_MS = 300;
const CHECK_POLLS = 10;
const CHECK_POLL_MS = 30;
const EXPIRED_ERROR = "browser action expired before the page ran it";
const STILL_LOADING_ERROR =
  "the page was still loading when the action's time ran out; nothing was changed. Take a snapshot and try again";
const STALE_PAGE_ERROR =
  "the page changed (it navigated) before the action could run; take a new snapshot and try again";
// Once part of an action reached the page, the rest is refused if the page
// navigates: a double-click's second press or a drag's moves would land on
// the new page at points measured on the old one.
const NAVIGATED_MID_ACTION_ERROR =
  "the page navigated partway through the action, so the rest of it wasn't sent; take a new snapshot to see where it is";
// Commands that deliver input to the page, as opposed to configuring the protocol.
const INPUT_DISPATCH = /^Input\.(dispatch|insertText)/;
const MODIFIER_NAMES = new Set(["Shift", "Control", "Alt", "Meta"]);

/**
 * How an input command relates to the action's target: "release" (a key or
 * button up, always let through so nothing stays held), "prepare" (a pointer
 * move with no button down, or a modifier press: leading up to the action,
 * which hasn't acted yet), or "act".
 */
function inputRole(method, params) {
  const type = params?.type;
  if (type === "keyUp" || type === "mouseReleased") return "release";
  if (method === "Input.dispatchMouseEvent" && type === "mouseMoved" && !params.buttons) {
    return "prepare";
  }
  if (method === "Input.dispatchKeyEvent" && MODIFIER_NAMES.has(params?.key)) return "prepare";
  return "act";
}
const FILE_CHOOSER_ERROR =
  "the click opened a file picker, which was cancelled: uploading files isn't supported";
// Inputs whose click opens a native picker over the user's screen.
const PICKER_INPUT_TYPES = new Set(["date", "datetime-local", "month", "time", "week", "color"]);
// Keys that open a focused <select>'s or picker's popup (arrows do on macOS).
const POPUP_KEYS = new Set([" ", "Enter", "ArrowUp", "ArrowDown", "F4"]);

const sleep = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

/**
 * Wait for a loading main frame to stop, and refuse if it commits a new
 * document meanwhile. Electron holds an executeJavaScript sent while the
 * main frame loads until it stops (waitTillCanExecuteJavaScript), so a
 * script sent mid-navigation would run, and change a form, on the page
 * that replaced the one the action was for. Sent once it isn't loading,
 * the script reaches the current document at once.
 */
async function untilMainFrameLoaded(webContents, deadlineAt) {
  if (typeof webContents.isLoadingMainFrame !== "function") return;
  if (!webContents.isLoadingMainFrame()) return;
  const unwatch = watchDocumentChange(webContents);
  try {
    while (webContents.isLoadingMainFrame()) {
      if (unwatch.changed()) throw new Error(STALE_PAGE_ERROR);
      if (Date.now() >= deadlineAt) throw new Error(STILL_LOADING_ERROR);
      // oxlint-disable-next-line no-await-in-loop -- Polled until the load ends.
      await sleep(SCROLL_POLL_MS);
    }
    if (unwatch.changed()) throw new Error(STALE_PAGE_ERROR);
  } finally {
    unwatch();
  }
}

/**
 * Run an action script in the page, giving up at `deadlineAt`. The script
 * itself also refuses to start after the deadline, so a page paused in
 * DevTools can't run an action late, after the agent was told it failed.
 *
 * @param {{ executeJavaScript: (js: string, userGesture?: boolean) => Promise<unknown> }} webContents
 * @param {string} js An expression from a page-script builder.
 * @param {{ deadlineAt?: number }} [opts] Epoch ms.
 */
async function runActionScript(
  webContents,
  js,
  { deadlineAt = Date.now() + DEFAULT_ACTION_MS } = {},
) {
  // Run inside an async function, so a script's thrown error reaches the
  // caller as a rejection with its message (a synchronous throw comes back
  // from executeJavaScript as a generic "Script failed to execute").
  // `omnigentLive()` is in the script's scope: mutating steps call it after page
  // handlers run (a focus handler may pause the page in DevTools), so they
  // stop at the deadline instead of finishing after the agent was told.
  const guarded =
    `(async () => { const omnigentLive = () => { if (Date.now() > ${Number(deadlineAt)}) ` +
    `throw new Error(${JSON.stringify(EXPIRED_ERROR)}); }; omnigentLive(); return ${js}; })()`;
  await untilMainFrameLoaded(webContents, deadlineAt);
  let timer;
  try {
    return await Promise.race([
      webContents.executeJavaScript(guarded, false),
      new Promise((_resolve, reject) => {
        timer = setTimeout(
          () => reject(new Error(PAGE_TIMEOUT_ERROR)),
          Math.max(0, deadlineAt - Date.now()),
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

// The protocol's object group for an action's pinned targets, released when it ends.
const PIN_GROUP = "omnigent-agent-targets";
// What the protocol says when an object's page (its execution context) is gone.
const GONE_OBJECT =
  /Could not find object|Cannot find context|context was destroyed|Inspected target navigated/i;

/** The page's error from a protocol evaluation, or null when it ran. */
function evaluationError(answer) {
  const details = answer?.exceptionDetails;
  if (!details) return null;
  const description = details.exception?.description ?? details.text ?? "the page script failed";
  // "Error: message\n    at ..." -> "message"
  return new Error(
    String(description)
      .split("\n")[0]
      .replace(/^[A-Za-z]*Error: /, ""),
  );
}

/**
 * Run an action script on a pinned node (`this` in the script), giving up at
 * `deadlineAt` as runActionScript does. A node whose page is gone (it
 * navigated) answers `goneError`.
 *
 * @param {{ sendCommand: Function }} dbg The action's debugger.
 * @param {{ isLoadingMainFrame?: () => boolean }} webContents
 * @param {{ objectId: string }} node From `pin`.
 * @param {string} js An expression from a page-script builder, built with `pinned`.
 * @param {{ deadlineAt: number, goneError: () => string }} opts
 */
async function runPinnedScript(dbg, webContents, node, js, { deadlineAt, goneError }) {
  const declaration =
    `async function () { const omnigentLive = () => { if (Date.now() > ${Number(deadlineAt)}) ` +
    `throw new Error(${JSON.stringify(EXPIRED_ERROR)}); }; omnigentLive(); return ${js}; }`;
  await untilMainFrameLoaded(webContents, deadlineAt);
  const call = dbg
    .sendCommand("Runtime.callFunctionOn", {
      objectId: node.objectId,
      functionDeclaration: declaration,
      awaitPromise: true,
      returnByValue: true,
    })
    .then(
      (answer) => {
        const error = evaluationError(answer);
        if (error) throw error;
        return answer?.result?.value;
      },
      (error) => {
        throw GONE_OBJECT.test(String(error?.message)) ? new Error(goneError()) : error;
      },
    );
  return beforeAgentDeadline(call, deadlineAt);
}

/**
 * Serialize debugger use per webContents; attach only for the action's
 * duration. The deadline counts from the request, including time spent queued
 * behind earlier actions, and the caller hears about it then even if the action
 * never got to run. After it, the action gets no debugger and its commands and
 * page scripts are refused, so a page that stalls (paused in DevTools) can't
 * receive input late or hold the debugger for later actions. File pickers the
 * action opens are intercepted and reported instead of shown.
 *
 * Targets are measured in the page that's showing. If the main frame starts
 * or commits a navigation to another document before the action's first
 * input (from webContents events, the protocol's Page.frameNavigated, or the
 * main frame's loaderId checked just before that input), its input and page
 * scripts are refused, so nothing meant for the old page lands on the new one. Pointer moves and modifier presses leading
 * up to the input don't count as its first input. Navigation after that is
 * the input's own doing (a link click), and the action finishes; releases
 * always go through, so no key or button stays held.
 */
const debuggerQueues = new WeakMap();
// Per webContents: the file-chooser interception left on after an action
// with trusted input, { attachedHere, end }, until the activation lapses.
const chooserGuards = new WeakMap();

/** Stop intercepting file choosers: detach a debugger this code attached,
 *  or hand a reused one its file chooser back. */
function endChooserInterception(real, attachedHere) {
  if (attachedHere) {
    try {
      real.detach();
    } catch {
      /* view destroyed mid-action */
    }
  } else {
    real.sendCommand("Page.setInterceptFileChooserDialog", { enabled: false }).catch(() => {});
  }
}

/**
 * Keep file choosers intercepted on `webContents` for the activation the
 * agent's input gave the page, so a picker its handler opens late never
 * reaches the user's screen. The user's own press or key ends it at once
 * (their upload is theirs), as do the debugger going away or the next
 * agent action, which takes it over.
 */
function guardFileChoosers(webContents, real, attachedHere, untilMs) {
  let ended = false;
  const end = ({ release = true } = {}) => {
    if (ended) return;
    ended = true;
    clearTimeout(timer);
    chooserGuards.delete(webContents);
    webContents.removeListener?.("before-mouse-event", onMouse);
    webContents.removeListener?.("before-input-event", onKey);
    real.removeListener?.("detach", onDetach);
    if (release) endChooserInterception(real, attachedHere);
  };
  const onMouse = (_event, pressed) => {
    if (pressed?.type === "mouseDown") end();
  };
  const onKey = (_event, input) => {
    if (input?.type === "keyDown" || input?.type === "rawKeyDown") end();
  };
  const onDetach = () => end({ release: false });
  const timer = setTimeout(end, Math.max(0, untilMs - Date.now()));
  timer.unref?.();
  webContents.on?.("before-mouse-event", onMouse);
  webContents.on?.("before-input-event", onKey);
  real.on?.("detach", onDetach);
  chooserGuards.set(webContents, { attachedHere, end, untilMs });
}
// Per webContents: { active, endedAt } for the agent actions holding the debugger.
const agentInputSpans = new WeakMap();

/**
 * Whether input arriving now on `webContents` may be an agent's: one of its
 * actions holds the debugger, or let go of it within AGENT_INPUT_GRACE_MS.
 * Native `input-event` fires for protocol input as well as the user's, so
 * listeners that treat input as proof of a human (design mode's gesture gate)
 * check this first.
 *
 * @param {object} webContents
 * @returns {boolean}
 */
function agentInputInFlight(webContents) {
  const span = webContents && agentInputSpans.get(webContents);
  return !!span && (span.active > 0 || Date.now() - span.endedAt < AGENT_INPUT_GRACE_MS);
}

/**
 * Watch `webContents` for its main frame leaving the current document.
 * Returns an unsubscribe function whose `.changed()` says whether it has.
 */
function watchDocumentChange(webContents) {
  let changed = false;
  // Electron passes a details event (newer) and positional flags (older).
  const onStart = (details, _url, isInPlace, isMainFrame) => {
    const mainFrame = details?.isMainFrame ?? isMainFrame;
    const sameDocument = details?.isSameDocument ?? isInPlace;
    if (mainFrame !== false && !sameDocument) changed = true;
  };
  // A main-frame commit to another document, including one already under way.
  const onCommit = () => {
    changed = true;
  };
  const subscribed = typeof webContents.on === "function";
  if (subscribed) {
    webContents.on("did-start-navigation", onStart);
    webContents.on("did-navigate", onCommit);
  }
  const unwatch = () => {
    if (!subscribed) return;
    webContents.removeListener("did-start-navigation", onStart);
    webContents.removeListener("did-navigate", onCommit);
  };
  unwatch.changed = () => changed;
  return unwatch;
}

function withDebugger(webContents, fn, { deadlineAt = Date.now() + DEFAULT_ACTION_MS } = {}) {
  let expired = false;
  let started = false;
  const previous = debuggerQueues.get(webContents) ?? Promise.resolve();
  const run = previous
    .catch(() => {})
    .then(async () => {
      // Its time ran out waiting behind an earlier action that stalled.
      if (expired || Date.now() >= deadlineAt) {
        expired = true;
        throw new Error(PAGE_TIMEOUT_ERROR);
      }
      started = true;
      const span = agentInputSpans.get(webContents) ?? { active: 0, endedAt: 0 };
      agentInputSpans.set(webContents, span);
      span.active += 1;
      const real = webContents.debugger;
      // An earlier action's interception, still on: this action takes it
      // over, and with it whether to detach at the end.
      const guard = chooserGuards.get(webContents);
      guard?.end({ release: false });
      // Its activation's time left, which this action keeps whatever it sends.
      const inheritedUntil = guard?.untilMs ?? 0;
      let attachedHere;
      try {
        attachedHere = guard && real.isAttached() ? guard.attachedHere : !real.isAttached();
        if (!real.isAttached()) real.attach("1.3");
      } catch (e) {
        span.active -= 1;
        span.endedAt = Date.now();
        throw e;
      }
      let fileChooser = false;
      let frameNavigated = false;
      // Keys and buttons pressed and not yet released, by key or button,
      // with the event that releases each, plus a native drag the page
      // started that hasn't been dropped; and whether drags are intercepted.
      const held = new Map();
      const onMessage = (_event, method, params) => {
        if (method === "Page.fileChooserOpened") fileChooser = true;
        // The page started a drag; until it's dropped or cancelled, the page
        // stays mid-drag.
        if (method === "Input.dragIntercepted") {
          held.set("drag", [
            "Input.dispatchDragEvent",
            {
              type: "dragCancel",
              x: 0,
              y: 0,
              data: params?.data ?? { items: [], dragOperationsMask: 0 },
            },
          ]);
        }
        // The protocol's own word that the main frame committed a new document.
        if (method === "Page.frameNavigated" && params?.frame && !params.frame.parentId) {
          frameNavigated = true;
        }
      };
      real.on("message", onMessage);
      const unwatchDocument = watchDocumentChange(webContents);
      let inputSent = false;
      // The main frame's document when the action started (see sendCommand).
      let startLoaderId;
      const mainLoaderId = async () =>
        (await real.sendCommand("Page.getFrameTree"))?.frameTree?.frame?.loaderId;
      const replaced = () => frameNavigated || unwatchDocument.changed();
      const changedError = () => (inputSent ? NAVIGATED_MID_ACTION_ERROR : STALE_PAGE_ERROR);
      const live = () => {
        if (expired) throw new Error(EXPIRED_ERROR);
        if (replaced()) throw new Error(changedError());
      };
      let interceptingDrags = false;
      let relinquished = false;
      // Whether `pin` resolved a target, whose handles the cleanup releases.
      let pinned = false;
      let automation;
      let verifiedInput = false;
      const track = (method, params) => {
        const type = params?.type;
        if (method === "Input.dispatchKeyEvent") {
          const id = `key:${params.code || params.key}`;
          if (type === "keyDown" || type === "rawKeyDown") {
            held.set(id, [
              method,
              {
                type: "keyUp",
                key: params.key,
                code: params.code,
                windowsVirtualKeyCode: params.windowsVirtualKeyCode,
                modifiers: 0,
              },
            ]);
          }
        } else if (method === "Input.dispatchMouseEvent" && params?.button) {
          const id = `mouse:${params.button}`;
          if (type === "mousePressed") {
            held.set(id, [
              method,
              {
                type: "mouseReleased",
                x: params.x,
                y: params.y,
                button: params.button,
                clickCount: 1,
              },
            ]);
          }
        } else if (method === "Input.setInterceptDrags" && params?.enabled === true) {
          interceptingDrags = true;
        }
      };
      // A release counts only once the page has taken it: one that fails
      // leaves the key, button or drag held for the cleanup to release again.
      const untrack = (method, params) => {
        const type = params?.type;
        if (method === "Input.dispatchKeyEvent" && type === "keyUp") {
          held.delete(`key:${params.code || params.key}`);
        } else if (method === "Input.dispatchMouseEvent" && params?.button) {
          if (type === "mouseReleased") held.delete(`mouse:${params.button}`);
        } else if (
          method === "Input.dispatchDragEvent" &&
          (type === "drop" || type === "dragCancel")
        ) {
          held.delete("drag");
        } else if (method === "Input.setInterceptDrags" && params?.enabled === false) {
          interceptingDrags = false;
        }
      };
      const dbg = {
        sendCommand: async (method, params, options) => {
          const role = INPUT_DISPATCH.test(method) ? inputRole(method, params) : null;
          const undoes = method === "Input.setInterceptDrags" && params?.enabled === false;
          // Once this action's cleanup starts, the debugger is about to be the
          // next action's: nothing this one sends may reach it, releases
          // included. The cleanup releases what's held itself.
          if (relinquished || (expired && (role === "release" || undoes))) {
            throw new Error(EXPIRED_ERROR);
          }
          if (role === "release" || undoes) {
            // Allowed while the action runs, whatever else it's refused: a
            // click whose press navigates still releases its button.
          } else if (role === "prepare") {
            // Moves and modifiers before the action acts don't commit it to the page.
            live();
          } else if (role === "act") {
            live();
            // Ask the page itself, right before each input, in case a
            // navigation's events haven't reached us yet.
            // A caller that has just done this itself (verifyDocument) skips
            // the round trip, so nothing can change between its last check
            // and the input.
            if (!options?.documentVerified && !verifiedInput) {
              const loaderId = startLoaderId === undefined ? undefined : await mainLoaderId();
              if (loaderId !== startLoaderId) throw new Error(changedError());
            }
            live();
            verifiedInput = false;
            inputSent = true;
          } else if (expired) {
            throw new Error(EXPIRED_ERROR);
          }
          // Recorded before sending: a press whose command stalls may still land.
          track(method, params);
          const answer = await real.sendCommand(
            method,
            params,
            typeof options === "string" ? options : undefined,
          );
          untrack(method, params);
          return answer;
        },
        /** The same page lookup that precedes each input, for a caller that
         *  checks something afterwards and then sends with `documentVerified`. */
        verifyDocument: async () => {
          live();
          const loaderId = startLoaderId === undefined ? undefined : await mainLoaderId();
          if (loaderId !== startLoaderId) throw new Error(changedError());
          live();
        },
        isAttached: () => real.isAttached(),
        // Used after the final focus check: no protocol read may move focus
        // between that check and the first trusted typing command.
        withVerifiedInput: async (sendInput) => {
          verifiedInput = true;
          try {
            return await sendInput();
          } finally {
            verifiedInput = false;
          }
        },
        on: (name, listener) => real.on(name, listener),
        removeListener: (name, listener) => real.removeListener(name, listener),
        /**
         * Resolve a target (ref / selector) to its node, once: the action's
         * later steps (`page` with `{ node }`) all run on that node, never on
         * whatever the selector or ref matches by then.
         */
        pin: async (target) => {
          live();
          const expression = buildAgentActionScript("pin", target);
          const answer = await beforeAgentDeadline(
            dbg.sendCommand("Runtime.evaluate", {
              expression,
              objectGroup: PIN_GROUP,
              returnByValue: false,
            }),
            deadlineAt,
          );
          const error = evaluationError(answer);
          if (error) throw error;
          if (!answer?.result?.objectId) throw new Error("the target isn't an element");
          pinned = true;
          return { objectId: answer.result.objectId };
        },
        /** Run a `buildAgentActionScript` action in the page and parse its JSON:
         *  on `args.node` (from `pin`) when given, as the script's target. */
        page: async (action, args) => {
          live();
          const { node, ...rest } = args ?? {};
          const raw = node
            ? await runPinnedScript(
                dbg,
                webContents,
                node,
                buildAgentActionScript(action, { ...rest, pinned: true }),
                { deadlineAt, goneError: changedError },
              )
            : await runActionScript(webContents, buildAgentActionScript(action, args), {
                deadlineAt,
              });
          return typeof raw === "string" && raw.startsWith("{") ? JSON.parse(raw) : raw;
        },
      };
      const work = (async () => {
        await dbg.sendCommand("Page.enable");
        startLoaderId = await mainLoaderId();
        await dbg.sendCommand("Page.setInterceptFileChooserDialog", { enabled: true });
        const connection = await automationDriver.connect(webContents, dbg, { deadlineAt });
        if (relinquished) {
          connection.disconnect();
          throw new Error(EXPIRED_ERROR);
        }
        automation = connection;
        dbg.automation = connection.page;
        const result = await fn(dbg);
        await sleep(FILE_CHOOSER_SETTLE_MS);
        if (fileChooser) throw new Error(FILE_CHOOSER_ERROR);
        return result;
      })();
      // The deadline may win the race; the rejection that follows is expected.
      work.catch(() => {});
      let timer;
      try {
        // At the deadline the debugger is released and the queue moves on,
        // even while a page call is still pending; that call's later return
        // finds every command refused.
        return await Promise.race([
          work,
          new Promise((_resolve, reject) => {
            timer = setTimeout(
              () => {
                expired = true;
                reject(new Error(PAGE_TIMEOUT_ERROR));
              },
              Math.max(0, deadlineAt - Date.now()),
            );
          }),
        ]);
      } finally {
        clearTimeout(timer);
        relinquished = true;
        real.removeListener("message", onMessage);
        unwatchDocument();
        // Release whatever an expired or failed action left held, and stop
        // intercepting drags, while the debugger is still ours. Bounded, so
        // a stalled page can't hold the queue.
        const undo = [...held.values()].reverse();
        if (interceptingDrags) undo.push(["Input.setInterceptDrags", { enabled: false }]);
        if (pinned) undo.push(["Runtime.releaseObjectGroup", { objectGroup: PIN_GROUP }]);
        // Every one is sent at once, in order (the protocol delivers commands
        // in the order sent), so one the page stalls on can't keep the rest
        // from going out; only the wait for their answers is bounded.
        const releases = undo.map(([method, params]) =>
          real.sendCommand(method, params).catch(() => {}),
        );
        if (releases.length > 0) {
          await Promise.race([Promise.all(releases), sleep(CLEANUP_WAIT_MS)]);
        }
        automation?.disconnect();
        // Input the page took as the user's may open a picker later on, as
        // may an earlier action's (a hover after a click sends none).
        const guardUntil = Math.max(
          inputSent && fileChooserGuard.ms > 0 ? Date.now() + fileChooserGuard.ms : 0,
          inheritedUntil,
        );
        if (guardUntil > Date.now()) guardFileChoosers(webContents, real, attachedHere, guardUntil);
        else endChooserInterception(real, attachedHere);
        span.active -= 1;
        span.endedAt = Date.now();
      }
    });
  debuggerQueues.set(webContents, run);
  // A queued action that times out before its turn fails the caller now, and
  // is skipped when its turn comes. A running one fails through its own
  // deadline above, once the debugger is released.
  run.catch(() => {});
  let timer;
  return Promise.race([
    run,
    new Promise((_resolve, reject) => {
      timer = setTimeout(
        () => {
          if (started) return;
          expired = true;
          reject(new Error(PAGE_TIMEOUT_ERROR));
        },
        Math.max(0, deadlineAt - Date.now()),
      );
    }),
  ]).finally(() => clearTimeout(timer));
}

async function locate(dbg, target, { scroll = "center", requireReachable = true } = {}) {
  const point = await dbg.page("locate", { ...target, scroll });
  if (!point.inView) throw new Error("element is outside the visible area after scrolling");
  if (requireReachable && point.covered) {
    throw new Error(`element is covered by ${point.covered} at its center`);
  }
  return point;
}

/** Pick `ref` / `selector` / `snapshot_id` (optionally `start_`/`end_` prefixed) out of args. */
function targetFrom(args, prefix = "") {
  const target = {};
  if (args[`${prefix}ref`] !== undefined) target.ref = args[`${prefix}ref`];
  if (args[`${prefix}selector`] !== undefined) target.selector = args[`${prefix}selector`];
  if (args.snapshot_id !== undefined && target.ref !== undefined) {
    target.snapshot_id = args.snapshot_id;
  }
  return target;
}

/** The target in `args` (see targetFrom), pinned to its node, as `{ node }`
 *  for `dbg.page`; `{}` for none (actions with an optional target). */
async function pinTarget(dbg, args, prefix = "") {
  const target = targetFrom(args, prefix);
  if (target.ref === undefined && target.selector === undefined) return {};
  return { node: await dbg.pin(target) };
}

/** `args` without its target (see targetFrom), for steps given a pinned one. */
function withoutTarget(args) {
  const { ref: _ref, selector: _selector, snapshot_id: _snapshotId, ...rest } = args;
  return rest;
}

function rejectUnknownArgs(args, allowed) {
  if (Object.keys(args).some((key) => !allowed.includes(key))) {
    throw new Error("Unsupported browser action argument");
  }
}

function mouse(dbg, type, point, extra = {}) {
  const pointer = dbg.automation.mouse;
  if (type === "mouseMoved") return pointer.move(point.x, point.y, { steps: extra.steps ?? 1 });
  if (type === "mousePressed") {
    return pointer.down({ button: extra.button, clickCount: extra.clickCount });
  }
  if (type === "mouseReleased") {
    return pointer.up({ button: extra.button, clickCount: extra.clickCount });
  }
  if (type === "mouseWheel") {
    // Puppeteer's wheel uses its last pointer position. Moving there first
    // would run hover handlers after our scroll-container measurement.
    return dbg.sendCommand("Input.dispatchMouseEvent", {
      type,
      x: point.x,
      y: point.y,
      button: "none",
      buttons: 0,
      modifiers: 0,
      deltaX: extra.deltaX,
      deltaY: extra.deltaY,
    });
  }
  throw new Error("Unsupported pointer input");
}

async function click(entry, dbg, args) {
  rejectUnknownArgs(args, ["ref", "snapshot_id", "selector", "button", "double_click"]);
  const button = args.button ?? "left";
  if (!["left", "right", "middle"].includes(button))
    throw new Error("button must be left, right or middle");
  if (args.double_click !== undefined && typeof args.double_click !== "boolean") {
    throw new Error("double_click must be a boolean");
  }
  const target = await pinTarget(dbg, args);
  const point = await locate(dbg, target);
  if (button === "left") refusePicker(point);
  // The view's native context menu would otherwise pop up over the user's
  // work. Suppressed for the whole action, however long measuring takes,
  // then for a moment after the last release: the protocol answers a mouse
  // event once the page has handled it, so its context menu is on its way.
  if (button === "right") entry.suppressContextMenuUntil = Infinity;
  try {
    const at = await pointAtRest(dbg, point, () => locate(dbg, target, { scroll: "none" }));
    // Hover effects can put a picker where the click lands.
    if (button === "left") refusePicker(at);
    for (let clickCount = 1; clickCount <= (args.double_click ? 2 : 1); clickCount += 1) {
      // The first click can replace the element or put something over it:
      // the second goes only to the same element, still at the same spot.
      // oxlint-disable-next-line no-await-in-loop -- Mouse events must arrive in order.
      if (clickCount === 2) await secondClickPoint(dbg, target, at, button);
      // oxlint-disable-next-line no-await-in-loop -- Mouse events must arrive in order.
      await mouse(dbg, "mousePressed", at, { button, clickCount });
      // oxlint-disable-next-line no-await-in-loop -- Mouse events must arrive in order.
      await mouse(dbg, "mouseReleased", at, { button, clickCount });
    }
  } finally {
    if (button === "right") entry.suppressContextMenuUntil = Date.now() + CONTEXT_MENU_SUPPRESS_MS;
  }
  return { ok: true };
}

/** Check that a double click's second click lands on its element again. */
async function secondClickPoint(dbg, target, at, button) {
  let now;
  try {
    now = await locate(dbg, target, { scroll: "none" });
  } catch (e) {
    throw new Error(
      `the first click changed the page (${e?.message ?? e}), so the second click was not sent; take a new snapshot to see what it did`,
      { cause: e },
    );
  }
  if (Math.abs(now.x - at.x) > 1 || Math.abs(now.y - at.y) > 1) {
    throw new Error(
      "the element moved after the first click, so the second click was not sent; take a new snapshot to see what it did",
    );
  }
  // The first click can have put a picker under the pointer.
  if (button === "left") refusePicker(now);
}

/**
 * Refuse a left click that would open a native popup over the user's screen:
 * on a <select> or picker input, or on whatever (a wrapper, a label) puts
 * one under the click point.
 */
function refusePicker(point) {
  for (const [tag, type, what] of [
    [point.tag, point.input_type, "element is"],
    [point.hit_tag, point.hit_input_type, "the click would land on"],
  ]) {
    if (tag === "select") {
      throw new Error(
        `${what} a native <select> — set it with browser_type, giving the option's value or label`,
      );
    }
    if (PICKER_INPUT_TYPES.has(type)) {
      throw new Error(`${what} an <input type=${type}> picker — use browser_type to set its value`);
    }
  }
}

// Pointer moves to try while a hover effect keeps moving the target.
const POINTER_SETTLE_TRIES = 3;

/**
 * Move the pointer onto `point`, then measure the target again: hover
 * effects (pointerenter handlers, :hover styles) can move it or cover it.
 * Follows it while it moves, and returns where it rests; `remeasure`
 * throws if it's now covered or off-screen.
 */
async function pointAtRest(dbg, point, remeasure) {
  let at = point;
  for (let attempt = 1; ; attempt += 1) {
    // oxlint-disable-next-line no-await-in-loop -- Each move depends on the last measurement.
    await mouse(dbg, "mouseMoved", at);
    // oxlint-disable-next-line no-await-in-loop -- Each move depends on the last measurement.
    const now = await remeasure(at);
    if (Math.abs(now.x - at.x) <= 1 && Math.abs(now.y - at.y) <= 1) return now;
    if (attempt === POINTER_SETTLE_TRIES) {
      throw new Error(
        "the element kept moving when the pointer reached it; take a new snapshot and try again",
      );
    }
    at = now;
  }
}

async function hover(entry, dbg, args) {
  rejectUnknownArgs(args, ["ref", "snapshot_id", "selector"]);
  const point = await locate(dbg, await pinTarget(dbg, args));
  await mouse(dbg, "mouseMoved", point);
  return { ok: true };
}

/**
 * Measure the drop target again where the pointer now is, and while it has
 * moved, call `moveTo` with where it is now and measure again. Returns where it rests; refuses, with nothing dropped,
 * if it's off screen, covered, or still moving.
 */
async function dropPointAtRest(dbg, endTarget, from, moveTo) {
  let to = from;
  for (let attempt = 1; ; attempt += 1) {
    // oxlint-disable-next-line no-await-in-loop -- Each move depends on the last measurement.
    const now = await dbg.page("locate", { ...endTarget, scroll: "none" });
    if (!now.inView) {
      throw new Error("the drop target moved off screen during the drag; nothing was dropped");
    }
    if (now.covered) {
      throw new Error(
        `the drop target is covered by ${now.covered} during the drag; nothing was dropped`,
      );
    }
    if (Math.abs(now.x - to.x) <= 1 && Math.abs(now.y - to.y) <= 1) return now;
    if (attempt === POINTER_SETTLE_TRIES) {
      throw new Error(
        "the drop target kept moving during the drag; take a new snapshot and try again; nothing was dropped",
      );
    }
    to = now;
    // oxlint-disable-next-line no-await-in-loop -- Each move depends on the last measurement.
    await moveTo(to);
  }
}

async function drag(entry, dbg, args) {
  rejectUnknownArgs(args, [
    "start_ref",
    "start_selector",
    "end_ref",
    "end_selector",
    "snapshot_id",
  ]);
  const source = await pinTarget(dbg, args, "start_");
  const dropTarget = await pinTarget(dbg, args, "end_");
  // Bring the drop target in first, then the source, so both end up visible.
  await locate(dbg, dropTarget, { scroll: "nearest", requireReachable: false });
  await locate(dbg, source, { scroll: "nearest" });
  // Measure both in the final layout, with no scrolling in between: pressing
  // at a point measured before a later scroll would grab whatever moved there.
  const start = await dbg.page("locate", { ...source, scroll: "none" });
  const end = await dbg.page("locate", { ...dropTarget, scroll: "none" });
  if (!start.inView || !end.inView) {
    throw new Error(
      "the drag source and drop target don't fit on screen together; scroll so both are visible, or move the item in smaller steps",
    );
  }
  if (start.covered) throw new Error(`drag source is covered by ${start.covered} at its center`);
  // Dropping there would land on whatever covers the target.
  if (end.covered) throw new Error(`drop target is covered by ${end.covered} at its center`);
  // Native HTML drag-and-drop would start an OS drag session; intercept it so
  // the drop can be replayed onto the target through the protocol instead.
  let dragData = null;
  const onMessage = (_event, method, params) => {
    if (method === "Input.dragIntercepted") dragData = params.data;
  };
  dbg.on("message", onMessage);
  try {
    await dbg.automation.setDragInterception(true);
    // A hover effect can move or cover the source once the pointer is on it.
    const from = await pointAtRest(dbg, start, () => locate(dbg, source, { scroll: "none" }));
    await mouse(dbg, "mousePressed", from, { button: "left", buttons: 1, clickCount: 1 });
    // The first step starts the drag; libraries then collapse the source,
    // insert placeholders or show overlays, which can move or cover the drop
    // target. Measure it again in that layout, and drop where it is now; a
    // refusal leaves the held button to withDebugger's cleanup.
    await mouse(dbg, "mouseMoved", {
      x: from.x + (end.x - from.x) / 5,
      y: from.y + (end.y - from.y) / 5,
    });
    let to = await locate(dbg, dropTarget, { scroll: "none" });
    await mouse(dbg, "mouseMoved", to, { steps: 4 });
    const dragEvent = (type) => {
      const pointer = dbg.automation.mouse;
      if (type === "dragEnter") return pointer.dragEnter(to, dragData);
      if (type === "dragOver") return pointer.dragOver(to, dragData);
      return pointer.drop(to, dragData);
    };
    if (dragData) {
      await dragEvent("dragEnter");
      await dragEvent("dragOver");
    }
    // The pointer arriving (dragenter, dragover, pointermove handlers) can
    // reorder a sortable list or put another zone where the target was:
    // follow the target until it rests, and drop only there.
    to = await dropPointAtRest(dbg, dropTarget, to, async (point) => {
      to = point;
      await mouse(dbg, "mouseMoved", to, { button: "left", buttons: 1 });
      if (dragData) await dragEvent("dragOver");
    });
    if (dragData) await dragEvent("drop");
    await mouse(dbg, "mouseReleased", to, { button: "left", clickCount: 1 });
  } finally {
    dbg.removeListener("message", onMessage);
    try {
      await dbg.automation.setDragInterception(false);
    } catch {
      /* view destroyed mid-drag */
    }
  }
  return { ok: true, html_drag: !!dragData };
}

async function pressKey(entry, dbg, args, platform) {
  rejectUnknownArgs(args, ["key", "ref", "snapshot_id", "selector"]);
  const combo = parseKeyCombo(args.key, platform);
  const unicode = [...combo.key].length === 1 && !/^[\x20-\x7e]$/.test(combo.key);
  const target = await pinTarget(dbg, args);
  const targeted = target.node !== undefined;
  // Where the key would go: still the target (focus handlers run after the
  // focus script; the key goes wherever focus is), and not a control whose
  // popup it would open over the user's screen, as a click on it would.
  // `keep` leaves the target for checking again.
  const checkKeyTarget = async ({ keep = false } = {}) => {
    if (targeted) await dbg.page("focus_confirm", keep ? { keep: true } : {});
    if (!POPUP_KEYS.has(combo.key)) return;
    const { tag, input_type: inputType } = await dbg.page("key_target", {});
    if (tag === "select" || PICKER_INPUT_TYPES.has(inputType)) {
      const what = tag === "select" ? "a native <select>" : `an <input type=${inputType}> picker`;
      throw new Error(
        `${args.key} on ${what} would open its popup — set it with browser_type instead; nothing was pressed`,
      );
    }
  };
  if (targeted) await dbg.page("focus", target);
  await checkKeyTarget({ keep: combo.modifiers.length > 0 });
  const keyboard = dbg.automation.keyboard;
  const held = [];
  try {
    for (const name of combo.modifiers) {
      // oxlint-disable-next-line no-await-in-loop -- Shortcut modifiers must precede the key.
      await keyboard.down(name);
      held.push(name);
    }
    if (held.length > 0) await checkKeyTarget();
    if (unicode) {
      await keyboard.sendCharacter(combo.key);
    } else {
      await keyboard.press(keyboardKey(combo));
    }
  } finally {
    for (const name of held.reverse()) {
      // oxlint-disable-next-line no-await-in-loop -- Release shortcuts in reverse order.
      await keyboard.up(name).catch(() => {});
    }
  }
  return { ok: true };
}

async function typeText(_entry, dbg, args) {
  rejectUnknownArgs(args, ["ref", "snapshot_id", "selector", "text"]);
  const { mode } = await dbg.page("type_prepare", {
    ...withoutTarget(args),
    ...(await pinTarget(dbg, args)),
  });
  if (mode === "set") return { ok: true };
  // Focus handlers run after the prepare script; the text goes wherever focus is.
  // The page lookup comes first, so no round trip separates the focus
  // confirmation from the input.
  await dbg.verifyDocument();
  await dbg.page("type_confirm", {});
  // Commit once, like an IME, rather than synthesize long-text DOM events.
  await dbg.withVerifiedInput(() =>
    args.text
      ? dbg.automation.keyboard.sendCharacter(args.text)
      : dbg.automation.keyboard.press("Backspace"),
  );
  return { ok: true };
}

async function check(_entry, dbg, args) {
  rejectUnknownArgs(args, ["ref", "snapshot_id", "selector", "checked"]);
  const want = args.checked !== false;
  const boxArgs = { ...withoutTarget(args), ...(await pinTarget(dbg, args)) };
  const target = await dbg.page("check_target", boxArgs);
  if (target.done) return { ok: true, checked: target.checked };
  // A hover effect can move or cover the box, or even set it.
  // The box chosen above, measured again: not the selector's match now.
  const at = await pointAtRest(dbg, target, async (last) => {
    const now = await dbg.page("check_target", { ...boxArgs, again: true });
    return now.done ? { ...last, ...now } : now;
  });
  if (at.done) return { ok: true, checked: at.checked };
  await mouse(dbg, "mousePressed", at, { button: "left", buttons: 1, clickCount: 1 });
  await mouse(dbg, "mouseReleased", at, { button: "left", clickCount: 1 });
  const { checked: _checked, ...stateArgs } = boxArgs;
  let state;
  // Frameworks may re-render aria-checked asynchronously after the click.
  for (let poll = 0; poll < CHECK_POLLS; poll += 1) {
    // oxlint-disable-next-line no-await-in-loop -- Polls must run in order.
    state = await dbg.page("check_state", { ...stateArgs, checked: want });
    if (state.checked === want) return { ok: true, checked: want };
    // oxlint-disable-next-line no-await-in-loop -- Polls must run in order.
    await sleep(CHECK_POLL_MS);
  }
  throw new Error(
    `clicking did not change its state; it is still ${state.checked ? "checked" : "unchecked"}`,
  );
}

async function scrollWheel(_entry, dbg, args) {
  rejectUnknownArgs(args, ["ref", "snapshot_id", "selector", "direction", "amount"]);
  const scrollArgs = { ...withoutTarget(args), ...(await pinTarget(dbg, args)) };
  const target = await dbg.page("scroll_target", scrollArgs);
  // Already as far as it goes: a wheel would scroll whatever is behind it.
  if (!target.room) {
    return {
      ok: true,
      scroll_x: target.scroll_x,
      scroll_y: target.scroll_y,
      at_end: true,
      container: target.container,
    };
  }
  if (target.x === null) {
    // No visible spot reaches the container without a nested scroller in the
    // way; scroll it directly rather than scroll the wrong thing.
    const result = await dbg.page("scroll", scrollArgs);
    return { ok: true, ...result };
  }
  await mouse(dbg, "mouseWheel", target, {
    deltaX: target.vertical ? 0 : target.delta,
    deltaY: target.vertical ? target.delta : 0,
  });
  // Wheel scrolling can animate; wait until the position stops changing.
  const started = Date.now();
  let state = target;
  let stable = 0;
  while (Date.now() - started < SCROLL_SETTLE_MAX_MS) {
    // oxlint-disable-next-line no-await-in-loop -- Polls must run in order.
    await sleep(SCROLL_POLL_MS);
    // oxlint-disable-next-line no-await-in-loop -- Polls must run in order.
    const next = await dbg.page("scroll_state", scrollArgs);
    const moved = next.pos !== target.pos;
    stable = next.pos === state.pos ? stable + 1 : 0;
    state = next;
    if (stable >= 2 && (moved || Date.now() - started >= SCROLL_START_WAIT_MS)) break;
  }
  // The wheel reached past the container (it ran out of room on the way).
  if (state.outer_moved) {
    throw new Error(
      `the wheel scrolled ${state.outer_moved}, not just ${state.container}; take a snapshot to see where the page is`,
    );
  }
  return {
    ok: true,
    scroll_x: state.scroll_x,
    scroll_y: state.scroll_y,
    // Right-to-left content's range starts below 0 (see POSITION_JS).
    at_end: target.delta > 0 ? state.pos >= state.max - 1 : state.pos <= (state.min ?? 0) + 1,
    container: state.container,
  };
}

const INPUT_HANDLERS = {
  click,
  hover,
  drag,
  press_key: pressKey,
  type: typeText,
  check,
  scroll: scrollWheel,
};

/**
 * Whether main runs an action with trusted protocol input (everything but
 * snapshot and select_option, which stay page scripts).
 *
 * @param {string} action
 */
function needsTrustedInput(action) {
  return Object.hasOwn(INPUT_HANDLERS, action);
}

/**
 * Run one trusted-input action against a registry entry's view.
 *
 * @param {{ view: { webContents: any }, suppressContextMenuUntil?: number }} entry
 * @param {string} action A key of `INPUT_HANDLERS`, e.g. `click` or `type`.
 * @param {Record<string, unknown>} args
 * @param {{ platform?: string, deadlineAt?: number }} [opts] `deadlineAt`: the
 *   caller's (epoch ms), from the request; the action stops there.
 * @returns {Promise<Record<string, unknown>>}
 */
function performBrowserInput(
  entry,
  action,
  args = {},
  { platform = process.platform, deadlineAt = Date.now() + DEFAULT_ACTION_MS } = {},
) {
  if (!Object.hasOwn(INPUT_HANDLERS, action)) throw new Error("Unsupported browser action");
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    throw new Error("Browser action arguments must be an object");
  }
  return withDebugger(
    entry.view.webContents,
    (dbg) => INPUT_HANDLERS[action](entry, dbg, args, platform),
    { deadlineAt },
  );
}

module.exports = {
  fileChooserGuard,
  AGENT_INPUT_GRACE_MS,
  agentInputInFlight,
  needsTrustedInput,
  parseKeyCombo,
  performBrowserInput,
  runActionScript,
};
