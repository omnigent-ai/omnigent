// Portable implementation of `window.omnigent`, injected as an inline script ahead of an
// uploaded app's HTML inside the sandboxed frame. The frame's CSP blocks network requests
// (`connect-src 'none'`) and it loads no external scripts, so the SDK travels as source
// text. It is plain ES2018 with no imports: bundler helpers or coverage counters would
// not exist inside the frame. `omnigentSdkTypes.ts` declares its public surface.
//
// Keep this source free of backticks, `${`, backslashes, and the sequence `</script`:
// it is embedded in a template literal and then in an HTML script element.

import {
  GADGET_CONNECT_MESSAGE_TYPE,
  GADGET_HELLO_MESSAGE_TYPE,
  GADGET_LIMITS,
  GADGET_METHOD_CAPABILITY,
  GADGET_MUTATING_METHODS,
  GADGET_PROTOCOL_VERSION,
} from "./gadgetProtocol";

export const OMNIGENT_SDK_RUNTIME_SOURCE = `(function (config) {
  'use strict';
  // WebRTC opens connections the frame's CSP does not cover, so its constructors go before the
  // gadget's script runs. Best effort: a nested about:blank frame the gadget creates has its own.
  ['RTCPeerConnection', 'webkitRTCPeerConnection', 'mozRTCPeerConnection', 'RTCIceTransport'].forEach(function (name) {
    try {
      Object.defineProperty(window, name, { value: undefined, writable: false, configurable: false });
    } catch (error) {
      // A browser without it, or one that does not allow redefining it.
    }
  });
  var LIMITS = config.limits;
  var PROTOCOL = config.protocol;
  var METHOD_CAPABILITY = config.methodCapability;
  var MUTATING = config.mutatingMethods;
  var KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/;
  var RETRYABLE_CODES = ['UNAVAILABLE', 'DEADLINE_EXCEEDED', 'RESOURCE_EXHAUSTED'];
  var TERMINAL_WATCH_CODES = ['PERMISSION_DENIED', 'UNSUPPORTED', 'REVISION_CHANGED', 'INVALID_ARGUMENT', 'UNAUTHENTICATED'];
  var trustedSources = [window.parent];
  if (window.parent && window.parent.parent && window.parent.parent !== window.parent) {
    trustedSources.push(window.parent.parent);
  }

  class OmnigentError extends Error {
    constructor(code, message, retryable, operationId) {
      super(message);
      Object.defineProperty(this, 'name', { value: 'OmnigentError' });
      this.code = code;
      this.retryable = Boolean(retryable);
      if (operationId) {
        this.operationId = operationId;
      }
    }
  }

  var port = null;
  var initContext = null;
  var connectWaiters = [];
  var clientPromise = null;
  var client = null;
  var disposed = false;
  var pending = new Map();
  var watches = new Set();
  var requestCounter = 0;

  function randomKey() {
    var bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    var out = '';
    for (var i = 0; i < bytes.length; i += 1) {
      out += (bytes[i] + 256).toString(16).slice(1);
    }
    return out;
  }

  function byteLength(value) {
    try {
      var json = JSON.stringify(value);
      return json === undefined ? -1 : new TextEncoder().encode(json).length;
    } catch (error) {
      return -1;
    }
  }

  function isJsonDocumentValue(value) {
    var remaining = LIMITS.maxJsonNodes;
    function visit(entry, depth) {
      remaining -= 1;
      if (remaining < 0 || depth > LIMITS.maxJsonDepth) return false;
      if (entry === null || typeof entry === 'string' || typeof entry === 'boolean') return true;
      if (typeof entry === 'number') {
        return Number.isFinite(entry) && (!Number.isInteger(entry) || Number.isSafeInteger(entry));
      }
      if (Array.isArray(entry)) {
        for (var i = 0; i < entry.length; i += 1) {
          if (!visit(entry[i], depth + 1)) return false;
        }
        return true;
      }
      if (typeof entry !== 'object') return false;
      var prototype = Object.getPrototypeOf(entry);
      if (prototype !== Object.prototype && prototype !== null) return false;
      var keys = Object.keys(entry);
      for (var j = 0; j < keys.length; j += 1) {
        if (!visit(entry[keys[j]], depth + 1)) return false;
      }
      return true;
    }
    try { return visit(value, 0); } catch (error) { return false; }
  }

  function clamp(value, fallback, min, max) {
    var number = typeof value === 'number' && isFinite(value) ? value : fallback;
    return Math.min(Math.max(number, min), max);
  }

  function rejected(code, message, operationId) {
    return Promise.reject(new OmnigentError(code, message, false, operationId));
  }

  function onPortMessage(event) {
    var data = event.data;
    if (!data || typeof data !== 'object' || data.v !== PROTOCOL) {
      return;
    }
    if (data.kind === 'init' && initContext === null && data.context && typeof data.context === 'object') {
      initContext = data.context;
      startPermissionGuards(initContext);
      var waiters = connectWaiters;
      connectWaiters = [];
      waiters.forEach(function (waiter) {
        waiter();
      });
      return;
    }
    if (data.kind === 'event' && data.event === 'agent.starting' && typeof data.starting === 'boolean') {
      setAgentStarting(data.starting);
      return;
    }
    if (data.kind !== 'response' || typeof data.id !== 'string') {
      return;
    }
    var entry = pending.get(data.id);
    if (!entry) {
      return;
    }
    entry.settle();
    if (data.ok === true) {
      entry.resolve(data.result);
    } else {
      var error = data.error && typeof data.error === 'object' ? data.error : {};
      var code = typeof error.code === 'string' ? error.code : 'INTERNAL';
      var message = typeof error.message === 'string' ? error.message : 'Request failed';
      var retryable = !entry.mutating && RETRYABLE_CODES.indexOf(code) !== -1;
      var failure = new OmnigentError(code, message, retryable, entry.operationId);
      if (typeof error.requestId === 'string') {
        failure.requestId = error.requestId;
      }
      entry.reject(failure);
    }
  }

  function onWindowMessage(event) {
    if (port !== null || disposed || trustedSources.indexOf(event.source) === -1) {
      return;
    }
    var data = event.data;
    if (!data || data.type !== config.connectType || data.nonce !== config.nonce || data.frameId !== config.frameId) {
      return;
    }
    if (!event.ports || event.ports.length !== 1) {
      return;
    }
    window.removeEventListener('message', onWindowMessage);
    port = event.ports[0];
    port.onmessage = onPortMessage;
    startSelectionReports();
  }

  // Text the viewer selects, or an element they Alt+click, goes to Omnigent, which offers the
  // session's Reply and Ask in side chat for it. The nearest [data-omnigent-ref] names what was
  // selected (with [data-omnigent-label]); \`occurrence\` tells repeated text in it apart.
  var lastSelection = null;
  var selectionTimer = null;
  function selectionScope(node) {
    var element = node && node.nodeType === 1 ? node : node && node.parentElement;
    return element && element.closest ? element.closest('[data-omnigent-ref]') : null;
  }
  function rectOf(target) {
    var rect = target && target.getBoundingClientRect ? target.getBoundingClientRect() : null;
    return rect ? { x: rect.left, y: rect.top, width: rect.width, height: rect.height } : { x: 0, y: 0, width: 0, height: 0 };
  }
  function occurrenceIn(scope, range, text) {
    try {
      var before = document.createRange();
      before.setStart(scope || document.body, 0);
      before.setEnd(range.startContainer, range.startOffset);
      var preceding = before.toString();
      var count = 1;
      var index = preceding.indexOf(text);
      while (index !== -1) {
        count += 1;
        index = preceding.indexOf(text, index + text.length);
      }
      return count;
    } catch (error) {
      return 1;
    }
  }
  function sendSelection(selection) {
    var key = JSON.stringify(selection);
    if (key === lastSelection || port === null || disposed) {
      return;
    }
    lastSelection = key;
    call('ui.v1.selection', { selection: selection }, {}).then(null, function () {});
  }
  function describe(scope, text, rect, occurrence) {
    var selection = { text: text.slice(0, 4000), rect: rect, occurrence: occurrence };
    if (scope) {
      selection.ref = String(scope.getAttribute('data-omnigent-ref')).slice(0, 200);
      var label = scope.getAttribute('data-omnigent-label');
      if (label) selection.label = label.slice(0, 200);
    }
    return selection;
  }
  function reportSelection() {
    selectionTimer = null;
    var selected = window.getSelection ? window.getSelection() : null;
    var text = selected && !selected.isCollapsed && selected.rangeCount > 0 ? selected.toString().trim() : '';
    if (!text) {
      sendSelection(null);
      return;
    }
    var range = selected.getRangeAt(0);
    var node = range.commonAncestorContainer;
    var element = node && node.nodeType === 1 ? node : node && node.parentElement;
    if (element && element.closest && element.closest('input, textarea, [contenteditable="true"]')) {
      sendSelection(null);
      return;
    }
    var scope = selectionScope(node);
    sendSelection(describe(scope, text, rectOf(range), occurrenceIn(scope, range, text)));
  }
  function scheduleSelectionReport(delay) {
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(reportSelection, delay);
  }
  function startSelectionReports() {
    if (typeof document === 'undefined' || typeof document.addEventListener !== 'function') {
      return;
    }
    // A new mouse or keyboard selection is reported even when it matches the last one, so the
    // host shows its buttons again after it dismissed them; "nothing selected" is not repeated.
    function forgetSelection() {
      if (lastSelection !== 'null') lastSelection = null;
    }
    document.addEventListener('mouseup', function () {
      forgetSelection();
      scheduleSelectionReport(0);
    });
    document.addEventListener('keyup', function (event) {
      if (event.shiftKey || event.key === 'Shift') {
        forgetSelection();
        scheduleSelectionReport(0);
      }
    });
    document.addEventListener('selectionchange', function () {
      var selected = window.getSelection ? window.getSelection() : null;
      if (!selected || selected.isCollapsed) scheduleSelectionReport(100);
    });
    document.addEventListener(
      'scroll',
      function () {
        sendSelection(null);
      },
      true
    );
    document.addEventListener(
      'click',
      function (event) {
        if (!event.altKey || !event.target || !event.target.closest) return;
        var target = event.target.closest('[data-omnigent-ref]') || event.target;
        var text = (target.getAttribute('data-omnigent-label') || target.innerText || target.textContent || '').trim();
        if (!text) return;
        event.preventDefault();
        event.stopPropagation();
        var scope = target.hasAttribute('data-omnigent-ref') ? target : selectionScope(target);
        sendSelection(describe(scope, text, rectOf(target), 1));
      },
      true
    );
  }

  function call(method, params, options, operationId) {
    options = options || {};
    if (disposed) {
      return rejected('DISPOSED', 'The Omnigent client was disposed', operationId);
    }
    if (port === null || initContext === null) {
      return rejected('UNAVAILABLE', 'The Omnigent host is not connected', operationId);
    }
    var capability = METHOD_CAPABILITY[method];
    if (capability === undefined || (capability !== null && initContext.capabilities.indexOf(capability) === -1)) {
      return rejected('UNSUPPORTED', 'This app revision does not have the ' + capability + ' capability', operationId);
    }
    if (options.signal && options.signal.aborted) {
      return rejected('CANCELLED', 'The call was cancelled', operationId);
    }
    if (pending.size >= LIMITS.maxInFlightRequests) {
      return rejected('RESOURCE_EXHAUSTED', 'Too many calls in flight', operationId);
    }
    if ((method === 'data.v1.create' || method === 'data.v1.replace') && !isJsonDocumentValue(params.value)) {
      return rejected('INVALID_ARGUMENT', 'Document value must be finite, bounded JSON without implicit conversions', operationId);
    }
    requestCounter += 1;
    var id = 'r' + requestCounter + '_' + randomKey().slice(0, 8);
    var deadlineMs = clamp(options.timeoutMs, LIMITS.defaultDeadlineMs, LIMITS.minDeadlineMs, LIMITS.maxDeadlineMs);
    var message = { v: PROTOCOL, kind: 'request', id: id, method: method, params: params, deadlineMs: deadlineMs };
    var size = byteLength(message);
    if (size < 0) {
      return rejected('INVALID_ARGUMENT', 'The request is not JSON-serializable', operationId);
    }
    // Send what was measured: JSON drops undefined properties (an omitted optional argument),
    // which a structured clone would keep and the host refuses.
    message = JSON.parse(JSON.stringify(message));
    if (size > LIMITS.maxMessageBytes) {
      return rejected('INVALID_ARGUMENT', 'The request exceeds ' + LIMITS.maxMessageBytes + ' bytes', operationId);
    }
    var mutating = MUTATING.indexOf(method) !== -1;
    return new Promise(function (resolve, reject) {
      var timer = null;
      var onAbort = null;
      var entry = {
        mutating: mutating,
        operationId: operationId,
        resolve: resolve,
        reject: reject,
        settle: function () {
          pending.delete(id);
          clearTimeout(timer);
          if (onAbort && options.signal) {
            options.signal.removeEventListener('abort', onAbort);
          }
        },
      };
      function abandon(code, text) {
        entry.settle();
        if (port !== null) {
          try { port.postMessage({ v: PROTOCOL, kind: 'cancel', id: id }); } catch (error) {
            // Cancellation is best-effort; a failed notice must not prevent local settlement.
          }
        }
        var outcome = mutating ? ' The change may still have been applied; retry with the same idempotency key.' : '';
        reject(new OmnigentError(code, text + outcome, !mutating && code === 'DEADLINE_EXCEEDED', operationId));
      }
      timer = setTimeout(function () {
        abandon('DEADLINE_EXCEEDED', 'The call did not finish within ' + deadlineMs + ' ms.');
      }, deadlineMs);
      if (options.signal) {
        onAbort = function () {
          abandon('CANCELLED', 'The call was cancelled.');
        };
        options.signal.addEventListener('abort', onAbort);
      }
      pending.set(id, entry);
      try {
        port.postMessage(message);
      } catch (error) {
        entry.settle();
        var code = error && error.name === 'DataCloneError' ? 'INVALID_ARGUMENT' : 'UNAVAILABLE';
        reject(new OmnigentError(code, 'The request could not be sent to Omnigent', !mutating && code === 'UNAVAILABLE', operationId));
      }
    });
  }

  function operationIdFrom(options) {
    var key = options && options.idempotencyKey;
    if (key === undefined) {
      return randomKey();
    }
    if (typeof key !== 'string' || key.length === 0 || key.length > LIMITS.maxOperationIdLength || !KEY_PATTERN.test(key)) {
      throw new OmnigentError('INVALID_ARGUMENT', 'idempotencyKey must be 1-' + LIMITS.maxOperationIdLength + ' characters of A-Z, a-z, 0-9, _ . : -, starting with a letter or digit');
    }
    return key;
  }

  function requireString(value, label, maxLength) {
    var limit = maxLength === undefined ? LIMITS.maxIdentifierLength : maxLength;
    if (typeof value !== 'string' || value.length === 0 || value.length > limit) {
      throw new OmnigentError('INVALID_ARGUMENT', label + ' must be a non-empty string of at most ' + limit + ' characters');
    }
    return value;
  }

  function guarded(run) {
    try {
      return run();
    } catch (error) {
      return Promise.reject(error);
    }
  }

  function makeCollection(name) {
    requireString(name, 'Collection name');
    var collection = {
      name: name,
      get: function (id, options) {
        return guarded(function () {
          return call('data.v1.get', { collection: name, id: requireString(id, 'id') }, options);
        });
      },
      list: function (page, options) {
        return guarded(function () {
          page = page || {};
          var params = { collection: name, limit: clamp(page.limit, LIMITS.maxListLimit, 1, LIMITS.maxListLimit) };
          if (page.cursor !== undefined) {
            params.cursor = requireString(page.cursor, 'cursor', 4096);
          }
          return call('data.v1.list', params, options);
        });
      },
      create: function (value, options) {
        return guarded(function () {
          var operationId = operationIdFrom(options);
          var params = { collection: name, value: value, operationId: operationId };
          if (options && options.id !== undefined) {
            params.id = requireString(options.id, 'id');
          }
          return call('data.v1.create', params, options, operationId);
        });
      },
      replace: function (id, value, options) {
        return guarded(function () {
          var operationId = operationIdFrom(options);
          var params = {
            collection: name,
            id: requireString(id, 'id'),
            value: value,
            ifVersion: requireString(options && options.ifVersion, 'ifVersion', 4096),
            operationId: operationId,
          };
          return call('data.v1.replace', params, options, operationId);
        });
      },
      delete: function (id, options) {
        return guarded(function () {
          var operationId = operationIdFrom(options);
          var params = {
            collection: name,
            id: requireString(id, 'id'),
            ifVersion: requireString(options && options.ifVersion, 'ifVersion', 4096),
            operationId: operationId,
          };
          return call('data.v1.delete', params, options, operationId);
        });
      },
      watch: function (listener, options) {
        // watch(options, listener) is a common guess; the order is unambiguous.
        if (typeof listener !== 'function' && typeof options === 'function') {
          var swapped = listener;
          listener = options;
          options = swapped;
        }
        if (typeof listener !== 'function') {
          throw new OmnigentError('INVALID_ARGUMENT', 'collection.watch needs a listener function. ' + COLLECTION_WATCH_USAGE);
        }
        options = options && typeof options === 'object' ? options : {};
        var intervalMs = clamp(options.intervalMs, 2000, LIMITS.minWatchIntervalMs, LIMITS.maxWatchIntervalMs);
        var limit = clamp(options.limit, LIMITS.maxListLimit, 1, LIMITS.maxListLimit);
        var active = true;
        var failures = 0;
        var timer = null;
        var controller = null;
        var snapshot = { status: 'loading', items: [], nextCursor: null, updatedAt: null, error: null };
        function emit() {
          var frozen = Object.freeze(Object.assign({}, snapshot));
          try {
            listener(frozen);
          } catch (error) {
            setTimeout(function () {
              throw error;
            }, 0);
          }
        }
        function schedule(delay) {
          if (!active) {
            return;
          }
          var hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden';
          timer = setTimeout(poll, hidden ? Math.min(delay * 5, LIMITS.maxWatchIntervalMs) : delay);
        }
        function poll() {
          if (!active) {
            return;
          }
          controller = new AbortController();
          collection.list({ limit: limit }, { signal: controller.signal }).then(
            function (page) {
              if (!active) {
                return;
              }
              failures = 0;
              snapshot = {
                status: 'live',
                items: page.items,
                nextCursor: page.nextCursor,
                updatedAt: new Date().toISOString(),
                error: null,
              };
              emit();
              schedule(intervalMs);
            },
            function (error) {
              if (!active || (error && (error.code === 'DISPOSED' || error.code === 'CANCELLED'))) {
                return;
              }
              failures += 1;
              var terminal = !error || TERMINAL_WATCH_CODES.indexOf(error.code) !== -1;
              snapshot = {
                status: terminal || failures >= LIMITS.watchStaleAfterFailures ? 'stale' : 'reconnecting',
                items: snapshot.items,
                nextCursor: snapshot.nextCursor,
                updatedAt: snapshot.updatedAt,
                error: error instanceof OmnigentError ? error : new OmnigentError('INTERNAL', String(error)),
              };
              emit();
              if (!terminal) {
                schedule(Math.min(intervalMs * Math.pow(2, failures), LIMITS.maxWatchBackoffMs));
              }
            }
          );
        }
        var handle = {
          dispose: function () {
            if (!active) {
              return;
            }
            active = false;
            clearTimeout(timer);
            if (controller) {
              controller.abort();
            }
            watches.delete(handle);
          },
        };
        watches.add(handle);
        emit();
        poll();
        return Object.freeze(handle);
      },
    };
    return Object.freeze(collection);
  }

  var COLLECTION_WATCH_USAGE = 'Call collection.watch(function (snapshot) { ... }, { intervalMs: 2000 }); it returns { dispose }. ' +
    'Each snapshot is { status, items, nextCursor, updatedAt, error }: status is loading, live, reconnecting or stale, ' +
    'and items holds the first page of records as { id, version, value, createdAt, updatedAt, createdBy, updatedBy }.';
  var ACTION_WATCH_USAGE = 'Call client.agent.watch(function (snapshot) { ... }, { actionIds: [receipt.actionId] }), ' +
    'or receipt.watch(function (snapshot) { ... }) on what agent.request resolves with; it returns { refresh, dispose }. ' +
    'Each snapshot is { status, items, action, updatedAt, error }: status is the watch state (loading, live, reconnecting or stale), ' +
    'items holds one { id, status, reason, summary, sessionId, requestedAt } per action, and action is that item when one action is watched. ' +
    'An action status is pending, queued, running or needs_input while it runs, and done, failed, cancelled, finished_unreported or interrupted once it ended.';

  function actionIdOf(value) {
    if (typeof value === 'string' && value !== '') {
      return value;
    }
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      if (typeof value.actionId === 'string') {
        return value.actionId;
      }
      if (typeof value.action_id === 'string') {
        return value.action_id;
      }
    }
    return null;
  }
  function actionIdsOf(value) {
    var one = actionIdOf(value);
    if (one !== null) {
      return [one];
    }
    if (Array.isArray(value)) {
      var ids = value.map(actionIdOf);
      return ids.every(function (id) { return id !== null; }) ? ids : null;
    }
    return null;
  }
  // The documented form is watch(listener, { actionIds }). The forms pages often guess are
  // unambiguous, so they work too: watch(actionId, listener), watch(listener, actionId),
  // watch(receipt, listener), and { actionId } or a single string in the options.
  function actionWatchArguments(first, second, third) {
    var listener;
    var target;
    var options;
    if (typeof first === 'function') {
      listener = first;
      target = second;
      options = third;
    } else if (typeof second === 'function') {
      listener = second;
      target = first;
      options = third;
    } else {
      throw new OmnigentError('INVALID_ARGUMENT', 'agent.watch needs a listener function; it is not an async iterator. ' + ACTION_WATCH_USAGE);
    }
    options = options && typeof options === 'object' ? options : {};
    var ids = null;
    if (target !== undefined && target !== null) {
      ids = actionIdsOf(target);
      if (ids === null) {
        if (typeof target !== 'object' || Array.isArray(target)) {
          throw new OmnigentError('INVALID_ARGUMENT', 'agent.watch got an action ID it cannot read. ' + ACTION_WATCH_USAGE);
        }
        options = Object.assign({}, target, options);
      }
    }
    if (ids === null) {
      var given = options.actionIds !== undefined ? options.actionIds : options.actionId;
      ids = given === undefined ? [] : actionIdsOf(given);
      if (ids === null) {
        throw new OmnigentError('INVALID_ARGUMENT', 'agent.watch options.actionIds must be a list of action IDs. ' + ACTION_WATCH_USAGE);
      }
    }
    return { listener: listener, options: { actionIds: ids, intervalMs: options.intervalMs } };
  }

  // Polls action progress, which Omnigent derives from the sessions doing the work.
  function watchActions(listener, options) {
    var intervalMs = clamp(options.intervalMs, 3000, LIMITS.minWatchIntervalMs, LIMITS.maxWatchIntervalMs);
    var actionIds = options.actionIds.slice();
    var active = true;
    var failures = 0;
    var timer = null;
    var controller = null;
    var snapshot = { status: 'loading', items: [], updatedAt: null, error: null };
    function watchedAction(items) {
      if (actionIds.length !== 1) {
        return null;
      }
      for (var i = 0; i < items.length; i += 1) {
        if (items[i] && items[i].id === actionIds[0]) {
          return items[i];
        }
      }
      return null;
    }
    function emit() {
      var frozen = Object.freeze(Object.assign({}, snapshot, { action: watchedAction(snapshot.items) }));
      try {
        listener(frozen);
      } catch (error) {
        setTimeout(function () {
          throw error;
        }, 0);
      }
    }
    function schedule(delay) {
      if (!active) {
        return;
      }
      var hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden';
      timer = setTimeout(poll, hidden ? Math.min(delay * 5, LIMITS.maxWatchIntervalMs) : delay);
    }
    // Each poll's number; only the latest may emit and schedule the next, so a listener that
    // calls refresh() leaves one poll running.
    var generation = 0;
    function poll() {
      if (!active) {
        return;
      }
      generation += 1;
      var mine = generation;
      controller = new AbortController();
      call('agent.v1.progress', { actionIds: actionIds }, { signal: controller.signal }).then(
        function (result) {
          if (!active || mine !== generation) {
            return;
          }
          failures = 0;
          snapshot = { status: 'live', items: result.items, updatedAt: new Date().toISOString(), error: null };
          emit();
          if (mine === generation) {
            schedule(intervalMs);
          }
        },
        function (error) {
          if (!active || mine !== generation || (error && (error.code === 'DISPOSED' || error.code === 'CANCELLED'))) {
            return;
          }
          failures += 1;
          var terminal = !error || TERMINAL_WATCH_CODES.indexOf(error.code) !== -1;
          snapshot = {
            status: terminal || failures >= LIMITS.watchStaleAfterFailures ? 'stale' : 'reconnecting',
            items: snapshot.items,
            updatedAt: snapshot.updatedAt,
            error: error instanceof OmnigentError ? error : new OmnigentError('INTERNAL', String(error)),
          };
          emit();
          if (!terminal && mine === generation) {
            schedule(Math.min(intervalMs * Math.pow(2, failures), LIMITS.maxWatchBackoffMs));
          }
        }
      );
    }
    var handle = {
      // Polls again now, for example right after a request so its first status shows at once.
      refresh: function () {
        if (!active) {
          return;
        }
        clearTimeout(timer);
        if (controller) {
          controller.abort();
        }
        poll();
      },
      dispose: function () {
        if (!active) {
          return;
        }
        active = false;
        clearTimeout(timer);
        if (controller) {
          controller.abort();
        }
        watches.delete(handle);
      },
    };
    watches.add(handle);
    emit();
    poll();
    return Object.freeze(handle);
  }

  function dispose() {
    if (disposed) {
      return;
    }
    disposed = true;
    Array.from(watches).forEach(function (watch) {
      watch.dispose();
    });
    Array.from(pending.entries()).forEach(function (pair) {
      var entry = pair[1];
      entry.settle();
      if (port !== null) {
        port.postMessage({ v: PROTOCOL, kind: 'cancel', id: pair[0] });
      }
      entry.reject(new OmnigentError('DISPOSED', 'The Omnigent client was disposed', false, entry.operationId));
    });
    window.removeEventListener('message', onWindowMessage);
    if (port !== null) {
      port.close();
    }
  }

  // Keep marked controls disabled, with a reason, when the viewer lacks access.
  var BLOCKED_REASONS = {
    write: 'You can view this gadget but not change it.',
    writeCapability: 'This gadget cannot change its records.',
    action: 'Running actions needs Can edit access to this gadget.',
    previewAction: 'Actions run once you use this template.',
  };
  var DISABLEABLE = ['BUTTON', 'SELECT', 'FIELDSET', 'OPTGROUP', 'OPTION'];
  var TYPED_INPUTS = ['text', 'search', 'email', 'url', 'tel', 'password', 'number', 'date', 'time', 'datetime-local', 'month', 'week'];
  var BLOCKED_EVENTS = ['click', 'dblclick', 'auxclick', 'dragstart', 'drop', 'paste', 'cut', 'input', 'change', 'submit'];

  // While the viewer's gadget session is being created, controls that run an action wait: the
  // session carries the requests made before Create session, and another would ask for a second
  // one. Only clicks are held and the root marked; the page's own disabled state is left alone,
  // so nothing needs restoring when the session is ready.
  var agentStarting = false;
  var startingListeners = [];
  var startingGuardInstalled = false;
  function holdActionWhileStarting(event) {
    if (!agentStarting) {
      return;
    }
    var target = event.target;
    var control = target && target.closest ? target.closest('[data-omnigent-requires~="action"]') : null;
    if (!control) {
      return;
    }
    if (event.type === 'keydown' && event.key !== 'Enter' && event.key !== ' ') {
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  function setAgentStarting(starting) {
    if (starting === agentStarting) {
      return;
    }
    agentStarting = starting;
    if (typeof document !== 'undefined' && document.documentElement) {
      if (!startingGuardInstalled && typeof document.addEventListener === 'function') {
        startingGuardInstalled = true;
        ['click', 'dblclick', 'submit', 'keydown'].forEach(function (type) {
          document.addEventListener(type, holdActionWhileStarting, true);
        });
        if (document.head) {
          var style = document.createElement('style');
          // :where keeps this at zero specificity, so the app's own styles win.
          style.textContent = ':where([data-omnigent-session-starting] [data-omnigent-requires~="action"]) { opacity: 0.55; cursor: progress; }';
          document.head.appendChild(style);
        }
      }
      if (starting) {
        document.documentElement.setAttribute('data-omnigent-session-starting', '');
        document.documentElement.setAttribute('aria-busy', 'true');
      } else {
        document.documentElement.removeAttribute('data-omnigent-session-starting');
        document.documentElement.removeAttribute('aria-busy');
      }
    }
    startingListeners.slice().forEach(function (listener) {
      try { listener(starting); } catch (error) { /* A failing listener can't stop the others. */ }
    });
  }
  var guardContext = null;
  function blockedReason(context, requirement) {
    if (!context) {
      return null;
    }
    if (requirement === 'write') {
      if (!context.permissions || !context.permissions.writeData) {
        return BLOCKED_REASONS.write;
      }
      return (context.capabilities || []).indexOf('data.write') === -1 ? BLOCKED_REASONS.writeCapability : null;
    }
    if (requirement === 'action') {
      if (context.preview === true) {
        return BLOCKED_REASONS.previewAction;
      }
      return context.permissions && context.permissions.publish ? null : BLOCKED_REASONS.action;
    }
    return null;
  }
  function elementBlockedReason(element) {
    var requirements = (element.getAttribute('data-omnigent-requires') || '').split(' ');
    for (var i = 0; i < requirements.length; i += 1) {
      var reason = blockedReason(guardContext, requirements[i]);
      if (reason) {
        return reason;
      }
    }
    return null;
  }
  function typedInput(element) {
    return element.tagName === 'TEXTAREA' || (element.tagName === 'INPUT' && TYPED_INPUTS.indexOf((element.type || 'text').toLowerCase()) !== -1);
  }
  function guardElement(element) {
    if (!element || element.nodeType !== 1 || !element.hasAttribute('data-omnigent-requires')) {
      return;
    }
    var reason = elementBlockedReason(element);
    if (!reason) {
      return;
    }
    // Only change what differs: every attribute write is observed, and an unchanged one would loop.
    if (!element.hasAttribute('data-omnigent-blocked')) {
      element.setAttribute('data-omnigent-blocked', '');
    }
    if (element.getAttribute('aria-disabled') !== 'true') {
      element.setAttribute('aria-disabled', 'true');
    }
    if (element.getAttribute('title') !== reason) {
      element.setAttribute('title', reason);
    }
    if (typedInput(element)) {
      // Read-only keeps the text selectable, so a viewer can still quote or comment on it.
      if (!element.readOnly) {
        element.readOnly = true;
      }
    } else if ((DISABLEABLE.indexOf(element.tagName) !== -1 || element.tagName === 'INPUT') && !element.disabled) {
      element.disabled = true;
    }
    if (element.getAttribute('contenteditable') !== null && element.getAttribute('contenteditable') !== 'false') {
      element.setAttribute('contenteditable', 'false');
    }
    if (element.getAttribute('draggable') === 'true') {
      element.setAttribute('draggable', 'false');
    }
  }
  function guardTree(node) {
    if (!node || node.nodeType !== 1) {
      return;
    }
    guardElement(node);
    var marked = node.querySelectorAll('[data-omnigent-requires]');
    for (var i = 0; i < marked.length; i += 1) {
      guardElement(marked[i]);
    }
  }
  function blockEvent(event) {
    var target = event.target;
    var blocked = target && target.closest ? target.closest('[data-omnigent-blocked]') : null;
    if (!blocked) {
      return;
    }
    if (event.type === 'keydown' && event.key !== 'Enter' && event.key !== ' ') {
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  function startPermissionGuards(context) {
    if (typeof document === 'undefined' || typeof document.querySelectorAll !== 'function' || typeof MutationObserver !== 'function') {
      return;
    }
    guardContext = context;
    if (!blockedReason(context, 'write') && !blockedReason(context, 'action')) {
      return;
    }
    BLOCKED_EVENTS.concat(['keydown']).forEach(function (type) {
      document.addEventListener(type, blockEvent, true);
    });
    if (document.head) {
      var style = document.createElement('style');
      // :where keeps this at zero specificity, so the app's own styles win.
      style.textContent = ':where([data-omnigent-blocked]) { opacity: 0.55; cursor: not-allowed; }';
      document.head.appendChild(style);
    }
    guardTree(document.documentElement);
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        if (record.type === 'attributes') {
          guardElement(record.target);
        } else {
          record.addedNodes.forEach(guardTree);
        }
      });
    }).observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['data-omnigent-requires', 'disabled', 'readonly', 'contenteditable', 'draggable', 'title', 'aria-disabled'],
    });
  }

  function makeClient(context) {
    var capabilities = Object.freeze(context.capabilities.slice());
    function hasCapability(capability) {
      return capabilities.indexOf(capability) !== -1;
    }
    return Object.freeze({
      context: Object.freeze({
        app: Object.freeze({ id: context.app.id, name: context.app.name }),
        revisionId: context.revisionId,
        viewer: Object.freeze({ id: context.viewer.id, displayName: context.viewer.displayName }),
        permissions: Object.freeze({
          writeData: Boolean(context.permissions.writeData),
          publish: Boolean(context.permissions.publish),
        }),
        preview: context.preview === true,
      }),
      capabilities: capabilities,
      hasCapability: hasCapability,
      can: function (requirement) {
        return blockedReason(context, requirement) === null;
      },
      data: Object.freeze({ collection: makeCollection }),
      sessions: Object.freeze({
        canList: hasCapability('sessions.read'),
        list: function (params, options) {
          return call('sessions.v1.list', params || {}, options);
        },
      }),
      navigation: Object.freeze({
        newSession: function (params, options) {
          params = params || {};
          return call('navigation.v1.newSession', { prompt: params.prompt, display: params.display }, options);
        },
        openSession: function (sessionId, options) {
          options = options || {};
          return call('navigation.v1.openSession', { sessionId: sessionId, display: options.display }, options);
        },
        newAutomation: function (params, options) {
          params = params || {};
          return call(
            'navigation.v1.newAutomation',
            { name: params.name, prompt: params.prompt, schedule: params.schedule, display: params.display },
            options,
          );
        },
      }),
      agent: Object.freeze({
        request: function (action, params, options) {
          // A retry passes the failed call's key (error.operationId, or its own
          // idempotencyKey) and gets the first request's receipt, never a second action.
          var operationId = operationIdFrom(options);
          return call('agent.v1.request', { action: action, params: params || {}, operationId: operationId }, options, operationId).then(function (receipt) {
            if (!receipt || typeof receipt.actionId !== 'string') {
              return receipt;
            }
            var actionId = receipt.actionId;
            // The receipt can follow its own action: receipt.watch(listener).
            return Object.freeze(Object.assign({}, receipt, {
              id: actionId,
              watch: function (listener, watchOptions) {
                var watched = actionWatchArguments(listener, watchOptions);
                return watchActions(watched.listener, { actionIds: [actionId], intervalMs: watched.options.intervalMs });
              },
            }));
          });
        },
        progress: function (actionIds, options) {
          return call('agent.v1.progress', { actionIds: actionIds || [] }, options);
        },
        watch: function (first, second, third) {
          var watched = actionWatchArguments(first, second, third);
          return watchActions(watched.listener, watched.options);
        },
        starting: function () {
          return agentStarting;
        },
        onStarting: function (listener) {
          if (typeof listener !== 'function') {
            throw new OmnigentError('INVALID_ARGUMENT', 'onStarting needs a listener function');
          }
          startingListeners.push(listener);
          return function () {
            var index = startingListeners.indexOf(listener);
            if (index !== -1) {
              startingListeners.splice(index, 1);
            }
          };
        },
      }),
      ui: Object.freeze({
        colorScheme: context.colorScheme === 'dark' ? 'dark' : 'light',
        blockedReason: function (requirement) {
          return blockedReason(context, requirement);
        },
      }),
      dispose: dispose,
    });
  }

  function connect(options) {
    if (disposed) {
      return rejected('DISPOSED', 'The Omnigent client was disposed');
    }
    if (clientPromise !== null) {
      return clientPromise;
    }
    var timeoutMs = clamp(options && options.timeoutMs, LIMITS.connectTimeoutMs, LIMITS.minDeadlineMs, LIMITS.maxDeadlineMs);
    clientPromise = new Promise(function (resolve, reject) {
      function ready() {
        clearTimeout(timer);
        if (client === null) {
          client = makeClient(initContext);
        }
        resolve(client);
      }
      var timer = setTimeout(function () {
        connectWaiters = connectWaiters.filter(function (waiter) {
          return waiter !== ready;
        });
        clientPromise = null;
        reject(new OmnigentError('UNAVAILABLE', 'The Omnigent host did not connect within ' + timeoutMs + ' ms', true));
      }, timeoutMs);
      if (initContext !== null) {
        ready();
      } else {
        connectWaiters.push(ready);
      }
    });
    return clientPromise;
  }

  Object.defineProperty(window, 'omnigent', {
    value: Object.freeze({
      version: 1,
      connect: connect,
      isOmnigentError: function (error) {
        return error instanceof OmnigentError;
      },
    }),
    writable: false,
    configurable: false,
    enumerable: true,
  });
  window.addEventListener('message', onWindowMessage);
  window.parent.postMessage({ type: config.helloType, frameId: config.frameId, nonce: config.nonce }, '*');
})`;

export interface OmnigentSdkBootstrapConfig {
  /** The sandbox iframe's element id; its messages are matched on it. */
  frameId: string;
  /** Per-mount random value the host checks in the hello and the frame checks in the connect message. */
  nonce: string;
}

// JSON is not safe inside an HTML script element as-is: `<` could close it, and U+2028/U+2029
// end statements in pre-ES2019 parsers.
function toInlineScriptJson(value: unknown): string {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .split(String.fromCharCode(0x2028))
    .join("\\u2028")
    .split(String.fromCharCode(0x2029))
    .join("\\u2029");
}

/** The inline script that installs `window.omnigent` and starts the handshake. */
export function buildOmnigentSdkBootstrapScript({
  frameId,
  nonce,
}: OmnigentSdkBootstrapConfig): string {
  const config = {
    frameId,
    nonce,
    protocol: GADGET_PROTOCOL_VERSION,
    helloType: GADGET_HELLO_MESSAGE_TYPE,
    connectType: GADGET_CONNECT_MESSAGE_TYPE,
    limits: GADGET_LIMITS,
    methodCapability: GADGET_METHOD_CAPABILITY,
    mutatingMethods: Array.from(GADGET_MUTATING_METHODS),
  };
  return `${OMNIGENT_SDK_RUNTIME_SOURCE}(${toInlineScriptJson(config)});`;
}
