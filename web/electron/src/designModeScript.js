// Design-mode picker script — the in-page driver injected into a WebContentsView
// via `executeJavaScript` to power point-and-prompt. Extracted from browserIpc.js
// so it lints/highlights as its own module; behavior is unchanged.
//
// Markers are prefixed with a per-enable `nonce`; the main-process handler
// trusts only markers carrying this view's nonce — keep the interpolation,
// don't hardcode the markers.

"use strict";

function buildDesignModeScript(nonce) {
  const SELECT = "__omni_" + nonce + "_element_select__";
  const SUBMIT = "__omni_" + nonce + "_element_prompt_submit__";
  const DISMISS = "__omni_" + nonce + "_element_dismiss__";
  return `
(function() {
  if (window.__omniDesignMode) return;
  window.__omniDesignMode = true;
  var __OMNI_SELECT = ${JSON.stringify(SELECT)};
  var __OMNI_SUBMIT = ${JSON.stringify(SUBMIT)};
  var __OMNI_DISMISS = ${JSON.stringify(DISMISS)};

  const overlay = document.createElement('div');
  overlay.id = '__omni-highlight';
  overlay.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483646;border:2px solid #c15f3c;background:rgba(193,95,60,0.08);transition:all 0.1s ease;display:none;';
  document.body.appendChild(overlay);
  const label = document.createElement('div');
  label.id = '__omni-label';
  label.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;background:#c15f3c;color:#fff;font:11px/1.4 -apple-system,sans-serif;padding:2px 6px;border-radius:3px;display:none;white-space:nowrap;';
  document.body.appendChild(label);

  const popup = document.createElement('div');
  popup.id = '__omni-popup';
  popup.style.cssText = [
    'position:fixed', 'display:none', 'z-index:2147483647',
    'background:rgba(28,28,30,0.96)', 'color:#f5f5f7',
    'border:1px solid rgba(255,255,255,0.12)', 'border-radius:12px',
    'box-shadow:0 10px 28px rgba(0,0,0,0.45)',
    'padding:10px 12px', 'min-width:280px', 'max-width:380px',
    'font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif',
    'font-size:13px', 'letter-spacing:-0.01em',
    'backdrop-filter:blur(20px)', '-webkit-backdrop-filter:blur(20px)',
  ].join(';') + ';';
  popup.innerHTML =
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">' +
      '<span id="__omni-popup-tag" style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#0a84ff;font-weight:600;"></span>' +
      '<span id="__omni-popup-text" style="flex:1;color:#aaaaae;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></span>' +
      '<button id="__omni-popup-close" type="button" style="background:none;border:none;color:#7c7c80;cursor:pointer;font-size:18px;line-height:1;padding:0 4px;font-family:inherit;">&times;</button>' +
    '</div>' +
    '<div id="__omni-popup-row" style="display:flex;gap:6px;">' +
      '<input id="__omni-popup-input" type="text" placeholder="What should change?" autocomplete="off" spellcheck="false" ' +
        'style="flex:1;padding:7px 10px;font-size:13px;border:1px solid rgba(255,255,255,0.14);border-radius:8px;background:rgba(0,0,0,0.32);color:#f5f5f7;outline:none;font-family:inherit;" />' +
      '<button id="__omni-popup-send" type="button" ' +
        'style="padding:7px 14px;background:#0a84ff;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;font-family:inherit;transition:opacity 0.12s;">Send</button>' +
    '</div>' +
    '<div id="__omni-popup-feedback" style="display:none;font-size:13px;font-weight:500;padding:4px 0;"></div>' +
    '<div id="__omni-popup-arrow" style="position:absolute;width:12px;height:12px;background:rgba(28,28,30,0.96);border:1px solid rgba(255,255,255,0.12);display:none;"></div>';
  document.body.appendChild(popup);

  const popupTag = popup.querySelector('#__omni-popup-tag');
  const popupText = popup.querySelector('#__omni-popup-text');
  const popupClose = popup.querySelector('#__omni-popup-close');
  const popupRow = popup.querySelector('#__omni-popup-row');
  const popupInput = popup.querySelector('#__omni-popup-input');
  const popupSend = popup.querySelector('#__omni-popup-send');
  const popupFeedback = popup.querySelector('#__omni-popup-feedback');
  const popupArrow = popup.querySelector('#__omni-popup-arrow');

  let currentEl = null;
  let activeEl = null;
  let popupVisible = false;
  let sending = false;

  function getReactComponent(el) {
    let fiber = null;
    for (const key of Object.keys(el)) {
      if (key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')) { fiber = el[key]; break; }
    }
    if (!fiber) return null;
    let node = fiber;
    for (let i = 0; i < 20 && node; i++) {
      if (node.type && typeof node.type === 'function') return node.type.displayName || node.type.name || null;
      if (node.type && typeof node.type === 'object' && node.type.render) return node.type.displayName || node.type.render.displayName || node.type.render.name || null;
      node = node.return;
    }
    return null;
  }

  function getElementInfo(el) {
    const rect = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const tag = el.tagName.toLowerCase();
    return {
      tag, id: el.id ? '#' + el.id : '',
      classes: el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).slice(0,3).join('.') : '',
      text: (el.textContent || '').trim().slice(0, 80),
      testId: el.getAttribute('data-testid') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      role: el.getAttribute('role') || '',
      component: getReactComponent(el),
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      styles: { color: cs.color, backgroundColor: cs.backgroundColor, fontSize: cs.fontSize, fontWeight: cs.fontWeight, padding: cs.padding, margin: cs.margin, display: cs.display, position: cs.position }
    };
  }

  function positionPopup(targetRect) {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const gap = 8;
    popup.style.left = '-9999px';
    popup.style.top = '0px';
    popup.style.display = 'block';
    const popW = popup.offsetWidth;
    const popH = popup.offsetHeight;
    let top = targetRect.bottom + gap;
    let arrowOnTop = true;
    if (top + popH > vh - 8) {
      top = targetRect.top - popH - gap;
      arrowOnTop = false;
    }
    if (top < 8) top = 8;
    let left = targetRect.left + (targetRect.width / 2) - (popW / 2);
    if (left < 8) left = 8;
    if (left + popW > vw - 8) left = vw - popW - 8;
    popup.style.left = left + 'px';
    popup.style.top = top + 'px';
    const arrowSize = 12;
    let arrowLeft = targetRect.left + (targetRect.width / 2) - left - (arrowSize / 2);
    if (arrowLeft < 10) arrowLeft = 10;
    if (arrowLeft > popW - arrowSize - 10) arrowLeft = popW - arrowSize - 10;
    popupArrow.style.display = 'block';
    popupArrow.style.left = arrowLeft + 'px';
    if (arrowOnTop) {
      popupArrow.style.top = (-arrowSize / 2 - 1) + 'px';
      popupArrow.style.bottom = '';
      popupArrow.style.borderRight = 'none';
      popupArrow.style.borderBottom = 'none';
      popupArrow.style.transform = 'rotate(45deg)';
    } else {
      popupArrow.style.bottom = (-arrowSize / 2 - 1) + 'px';
      popupArrow.style.top = '';
      popupArrow.style.borderLeft = 'none';
      popupArrow.style.borderTop = 'none';
      popupArrow.style.transform = 'rotate(45deg)';
    }
  }

  let submitId = 0;
  let resultTimer = null;

  function resetInputRow() {
    popupRow.style.display = 'flex';
    popupFeedback.style.display = 'none';
    popupFeedback.textContent = '';
    popupInput.value = '';
    popupInput.disabled = false;
    popupSend.disabled = false;
    popupSend.textContent = 'Send';
    popupSend.style.opacity = '1';
    popupSend.style.cursor = 'pointer';
  }

  function showPopup(el, info) {
    activeEl = el;
    const niceTag = info.component ? '<' + info.component + '>' : '<' + info.tag + '>';
    popupTag.textContent = niceTag;
    popupText.textContent = info.text ? '\\u201c' + info.text.slice(0, 40) + '\\u201d' : '';
    resetInputRow();
    sending = false;
    positionPopup(el.getBoundingClientRect());
    popupVisible = true;
    overlay.style.left = info.rect.x + 'px';
    overlay.style.top = info.rect.y + 'px';
    overlay.style.width = info.rect.width + 'px';
    overlay.style.height = info.rect.height + 'px';
    overlay.style.display = 'block';
    setTimeout(function() { popupInput.focus(); popupInput.select(); }, 30);
  }

  function hidePopup(emitDismiss) {
    if (resultTimer) { clearTimeout(resultTimer); resultTimer = null; }
    popup.style.display = 'none';
    activeEl = null;
    popupVisible = false;
    sending = false;
    popupRow.style.display = 'flex';
    popupFeedback.style.display = 'none';
    popupInput.disabled = false;
    popupSend.disabled = false;
    if (emitDismiss) console.log(__OMNI_DISMISS);
  }

  function showFeedback(ok, message) {
    popupRow.style.display = 'none';
    popupFeedback.textContent = message;
    popupFeedback.style.color = ok ? '#30d158' : '#ff453a';
    popupFeedback.style.display = 'block';
  }

  window.__omniOnDesignResult = function(result) {
    if (!result || result.id !== submitId) return;
    if (!popupVisible || !sending) return;
    showFeedback(!!result.ok, String(result.message || (result.ok ? 'Applied.' : 'Failed.')));
    if (resultTimer) clearTimeout(resultTimer);
    resultTimer = setTimeout(function() { hidePopup(false); }, result.ok ? 900 : 2400);
  };

  function submitPopup() {
    if (sending) return;
    const text = popupInput.value.trim();
    if (!text || !activeEl) return;
    sending = true;
    submitId += 1;
    const id = submitId;
    popupSend.textContent = 'Sending\\u2026';
    popupSend.disabled = true;
    popupSend.style.opacity = '0.6';
    popupSend.style.cursor = 'default';
    popupInput.disabled = true;
    const info = getElementInfo(activeEl);
    console.log(__OMNI_SUBMIT + JSON.stringify({ id: id, element: info, prompt: text }));
    if (resultTimer) clearTimeout(resultTimer);
    resultTimer = setTimeout(function() {
      if (!popupVisible || !sending || submitId !== id) return;
      showFeedback(false, 'No response (timed out).');
      resultTimer = setTimeout(function() { hidePopup(false); }, 1500);
    }, 8000);
  }

  popupClose.addEventListener('click', function(e) { e.stopPropagation(); hidePopup(true); });
  popupSend.addEventListener('click', function(e) { e.stopPropagation(); submitPopup(); });
  popupInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitPopup(); return; }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); hidePopup(true); }
  });

  function onMouseMove(e) {
    if (popupVisible) return;
    const el = document.elementFromPoint(e.clientX, e.clientY);
    if (!el || el === overlay || el === label) return;
    if (popup.contains(el)) return;
    currentEl = el;
    const rect = el.getBoundingClientRect();
    overlay.style.display = 'block';
    overlay.style.left = rect.left + 'px'; overlay.style.top = rect.top + 'px';
    overlay.style.width = rect.width + 'px'; overlay.style.height = rect.height + 'px';
    const component = getReactComponent(el);
    const tag = el.tagName.toLowerCase();
    const cls = el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/)[0] : '';
    label.textContent = (component ? '<' + component + '> ' : '') + tag + cls;
    label.style.display = 'block';
    label.style.left = rect.left + 'px'; label.style.top = Math.max(0, rect.top - 22) + 'px';
  }
  function onClick(e) {
    if (popup.contains(e.target)) return;
    let el = currentEl;
    if (popupVisible) {
      const hit = document.elementFromPoint(e.clientX, e.clientY);
      if (hit && hit !== overlay && hit !== label && !popup.contains(hit)) el = hit;
    }
    if (!el) return;
    e.preventDefault(); e.stopPropagation();
    currentEl = el;
    window.__omniSelectedEl = el;
    const info = getElementInfo(el);
    console.log(__OMNI_SELECT + JSON.stringify(info));
    showPopup(el, info);
  }
  document.addEventListener('mousemove', onMouseMove, true);
  document.addEventListener('click', onClick, true);

  window.__omniDisableDesignMode = function() {
    document.removeEventListener('mousemove', onMouseMove, true);
    document.removeEventListener('click', onClick, true);
    if (resultTimer) { clearTimeout(resultTimer); resultTimer = null; }
    overlay.remove(); label.remove(); popup.remove();
    delete window.__omniDesignMode;
    delete window.__omniDisableDesignMode;
    delete window.__omniOnDesignResult;
  };
})();
`;
}

module.exports = { buildDesignModeScript };
