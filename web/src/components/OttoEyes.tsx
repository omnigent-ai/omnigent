import { useEffect, useRef } from "react";
import { OttoIcon } from "@/components/icons/OttoIcon";

// Eye geometry in the SVG's own viewBox coordinate system (0 0 1024 1024).
// Each of Otto's eyes is a fixed near-circular white with a concentric pupil
// drawn on top; the pupil can slide until its rim meets the inner edge of
// the white, i.e. up to (whiteRadius - pupilRadius) away from the eye center.
const VIEWBOX_W = 1024;
const VIEWBOX_H = 1024;
// Radii measured off the main-eye paths in OttoIcon.tsx.
const WHITE_RADIUS = 71.3;
const PUPIL_RADIUS = 55.9;
// How far a pupil may travel before its edge touches the white rim. Capped
// well below that geometric max (~15.4) to keep the same travel-to-eye-size
// ratio (~13% of the white radius) as the previous mascots.
const MAX_OFFSET = Math.min(9.3, WHITE_RADIUS - PUPIL_RADIUS);

// Centers of Otto's two eyes in `g.otto-pupil` document order — right eye
// first, then left, matching OttoIcon.tsx. The buddy starfish has no pupil
// groups, so its eyes stay still.
const EYE_CENTERS = [
  { cx: 619.1, cy: 520.6 },
  { cx: 413.8, cy: 520.6 },
];

type ScreenPoint = { x: number; y: number };
type TextField = HTMLTextAreaElement | HTMLInputElement;

// <input> types that hold text a caret moves through. Buttons, checkboxes,
// ranges, etc. carry no caret, so focusing them keeps Otto on the pointer.
const CARET_INPUT_TYPES = new Set([
  "text",
  "search",
  "url",
  "tel",
  "email",
  "password",
  "number",
  "",
]);

// Text fields whose caret Otto should follow while focused: <textarea>, a
// text-bearing <input>, or a contenteditable host.
function isTrackedTextField(node: EventTarget | null): node is HTMLElement {
  if (!(node instanceof HTMLElement)) return false;
  if (node instanceof HTMLTextAreaElement) return true;
  if (node instanceof HTMLInputElement) return CARET_INPUT_TYPES.has(node.type);
  return node.isContentEditable;
}

// Screen-space point of the caret inside a <textarea>/<input>. There is no
// native caret-rect API, so a hidden <div> mirrors the field's text layout and
// a DOM Range measures the real character before the caret. Two details make
// the mirror wrap *identically* to the field (earlier bugs made it wrap a word
// early, so Otto glanced a line too low):
//   - the font is copied via the `font` SHORTHAND; copying individual longhand
//     properties lets an inherited font-stretch/variation widen the mirror's
//     text and wrap it early;
//   - getComputedStyle().width is the content-box width, so the mirror uses
//     box-sizing:content-box with width = clientWidth - horizontal padding and
//     zero padding/border, rather than copying width onto a border-box element.
// Only the direction to the caret matters (the pupil is normalized onto the eye
// rim), so sub-pixel differences are invisible.
function textFieldCaretPoint(el: TextField): ScreenPoint | null {
  const doc = el.ownerDocument;
  const win = doc.defaultView;
  if (!win) return null;

  const cs = win.getComputedStyle(el);
  const padLeft = parseFloat(cs.paddingLeft) || 0;
  const padRight = parseFloat(cs.paddingRight) || 0;
  const padTop = parseFloat(cs.paddingTop) || 0;
  const borderLeft = parseFloat(cs.borderLeftWidth) || 0;
  const borderTop = parseFloat(cs.borderTopWidth) || 0;
  const contentWidth = el.clientWidth - padLeft - padRight;

  const mirror = doc.createElement("div");
  // The `font` shorthand carries family/size/weight/style/line-height together
  // and resets the rest, so the mirror's glyph metrics match the field exactly.
  mirror.style.font = cs.font;
  mirror.style.letterSpacing = cs.letterSpacing;
  mirror.style.wordSpacing = cs.wordSpacing;
  mirror.style.textTransform = cs.textTransform;
  mirror.style.textIndent = cs.textIndent;
  mirror.style.tabSize = cs.tabSize;
  mirror.style.direction = cs.direction;
  mirror.style.position = "absolute";
  mirror.style.top = "0";
  mirror.style.left = "0";
  mirror.style.boxSizing = "content-box";
  mirror.style.width = `${contentWidth}px`;
  mirror.style.height = "auto";
  mirror.style.padding = "0";
  mirror.style.border = "0";
  mirror.style.visibility = "hidden";
  mirror.style.whiteSpace = el instanceof HTMLInputElement ? "pre" : "pre-wrap";
  mirror.style.wordWrap = "break-word";
  mirror.style.overflowWrap = "break-word";
  mirror.textContent = el.value;

  // Default to the content box's top-left (caret at the very start, empty
  // field, or a layout-less environment like jsdom where Range rects are
  // unavailable). A real character rect overrides it below.
  let localLeft = 0;
  let localTop = 0;
  let caretHeight = parseFloat(cs.lineHeight) || 0;

  // Always remove the mirror, even if a measurement throws (a browser Range
  // quirk, or the field detaching mid-frame): this runs on every tracked
  // event via rAF, so a leaked node would accumulate one hidden <div> per
  // frame and the uncaught throw would kill all subsequent tracking.
  doc.body.appendChild(mirror);
  try {
    const node = mirror.firstChild;
    // Clamp against the mirrored text length: selectionStart can momentarily
    // lead el.value (controlled inputs, IME composition), and an out-of-range
    // index would make Range.setStart throw.
    const textLength = node?.textContent?.length ?? 0;
    const caretIndex = Math.min(el.selectionStart ?? el.value.length, textLength);
    const mirrorRect = mirror.getBoundingClientRect();

    if (node && caretIndex > 0) {
      // The character before the caret; its trailing edge is where the caret
      // rides, so the caret only drops to the next line once that glyph wraps.
      // getClientRects() splits at a line wrap \u2014 the last rect is on the caret's
      // line.
      const range = doc.createRange();
      range.setStart(node, caretIndex - 1);
      range.setEnd(node, caretIndex);
      const rects = typeof range.getClientRects === "function" ? range.getClientRects() : null;
      const r =
        rects && rects.length > 0
          ? rects[rects.length - 1]
          : typeof range.getBoundingClientRect === "function"
            ? range.getBoundingClientRect()
            : null;
      if (r && (r.width > 0 || r.height > 0 || r.right > 0 || r.top > 0)) {
        localLeft = r.right - mirrorRect.left;
        localTop = r.top - mirrorRect.top;
        caretHeight = r.height;
      }
    }
  } finally {
    doc.body.removeChild(mirror);
  }

  const rect = el.getBoundingClientRect();
  // The mirror's zero-padding/zero-border content box starts at its own
  // top-left, so a local offset maps to the field's content origin (border +
  // padding inside the field's border box), adjusted for the field's scroll.
  return {
    x: rect.left + borderLeft + padLeft + localLeft - el.scrollLeft,
    y: rect.top + borderTop + padTop + localTop - el.scrollTop + caretHeight / 2,
  };
}

// Screen-space point of the caret in a contenteditable host, taken from the
// collapsed selection's client rect. Returns null when there's no live
// caret rect to read.
function contentEditableCaretPoint(win: Window): ScreenPoint | null {
  const sel = win.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0).cloneRange();
  range.collapse(false);
  const rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
  if (!rect || (rect.width === 0 && rect.height === 0 && rect.left === 0 && rect.top === 0)) {
    return null;
  }
  return { x: rect.left, y: rect.top + rect.height / 2 };
}

// Where the focused text field's caret sits on screen, or null if it can't be
// resolved (whereupon Otto falls back to the pointer).
function caretPointFor(field: HTMLElement): ScreenPoint | null {
  if (field instanceof HTMLTextAreaElement || field instanceof HTMLInputElement) {
    return textFieldCaretPoint(field);
  }
  const win = field.ownerDocument.defaultView;
  return win ? contentEditableCaretPoint(win) : null;
}

/**
 * The Omnigent starfish mascot (Otto) with eyes that follow the cursor: each
 * black pupil slides to the inner edge of its white eye on the side nearest
 * whatever Otto is watching.
 *
 * Otto looks at whatever the user last moved: the mouse pointer, or — while a
 * text field (textarea, text input, or contenteditable) is focused — its
 * **text caret**. Moving the mouse pulls his gaze to the pointer even while a
 * field is focused; a genuine caret move (typing, paste/delete, arrow/Home/End
 * navigation, click-to-reposition) pulls it back. The pupils' 90ms transform
 * transition (below) smooths every hand-off.
 *
 * On mount the pupils sit centered (no transform written) — focusing a field
 * (including the composer's autofocus) only marks it eligible for
 * caret-tracking; Otto starts watching only once the user actually moves the
 * mouse or the caret.
 *
 * The art lives in OttoIcon; this component drives its two `g.otto-pupil`
 * groups (black disc + glint) through the forwarded ref. Updates are
 * coalesced into a single rAF callback and applied straight to the DOM
 * nodes, so tracking never re-renders React. Respects
 * `prefers-reduced-motion` by leaving the pupils centered.
 */
export function OttoEyes({ className }: { className?: string }) {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;

    // Class-selector contract with OttoIcon (pinned by OttoIcon.test.tsx);
    // querySelectorAll fails silently, so a rename would freeze the eyes.
    const pupils = Array.from(svg.querySelectorAll<SVGGElement>("g.otto-pupil"));
    for (const pupil of pupils) {
      // Smooths each pupil's slide toward its target rather than snapping.
      pupil.style.transition = "transform 90ms ease-out";
      pupil.style.willChange = "transform";
    }

    let frame = 0;
    let pointer: ScreenPoint | null = null;
    // The focused text field whose caret Otto can watch, or null when no
    // tracked field is focused.
    let activeField: HTMLElement | null = null;
    // What the user last moved decides where Otto looks. null (nothing yet)
    // keeps the pupils centered — Otto only starts tracking on the first
    // genuine activity, never on mount or on the composer's autofocus alone.
    let source: "pointer" | "caret" | null = null;

    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(apply);
    };

    const apply = () => {
      frame = 0;
      // React can unmount the focused field (route change, conditional render)
      // without always delivering a matching focusout, leaving activeField
      // pointing at a detached node whose rect measures as (0,0) — Otto would
      // aim at the top-left corner. Drop a disconnected field back to the
      // pointer so he rests centered instead.
      if (activeField && !activeField.isConnected) {
        activeField = null;
        if (source === "caret") source = "pointer";
      }
      // Follow the last-moved source; if the caret can't be resolved, fall
      // back to the pointer. Neither source yet → leave the pupils centered.
      const target =
        source === "caret" && activeField
          ? (caretPointFor(activeField) ?? pointer)
          : source === "pointer"
            ? pointer
            : null;
      if (!target) {
        // Tracking started but there's nothing to aim at yet (e.g. blurred
        // before the mouse ever moved): rest the pupils centered rather than
        // leaving them stuck at the last offset. On mount (source === null)
        // nothing is written, so the pupils stay untouched.
        if (source !== null) {
          for (const pupil of pupils) pupil.style.transform = "translate(0px, 0px)";
        }
        return;
      }
      const rect = svg.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      EYE_CENTERS.forEach((eye, i) => {
        const pupil = pupils[i];
        if (!pupil) return;
        // Eye center in screen space. The viewBox maps uniformly into the
        // rendered box (matching aspect ratio, default preserveAspectRatio),
        // so a single scale per axis is exact.
        const eyeX = rect.left + (eye.cx / VIEWBOX_W) * rect.width;
        const eyeY = rect.top + (eye.cy / VIEWBOX_H) * rect.height;
        const dx = target.x - eyeX;
        const dy = target.y - eyeY;
        const dist = Math.hypot(dx, dy);
        if (dist < 0.0001) {
          pupil.style.transform = "translate(0px, 0px)";
          return;
        }
        // Always ride the rim toward the target. translate() px units on an
        // SVG element resolve to user-space units, so MAX_OFFSET is correct.
        const tx = (dx / dist) * MAX_OFFSET;
        const ty = (dy / dist) * MAX_OFFSET;
        pupil.style.transform = `translate(${tx.toFixed(3)}px, ${ty.toFixed(3)}px)`;
      });
    };

    const onMove = (e: PointerEvent) => {
      pointer = { x: e.clientX, y: e.clientY };
      // The mouse just moved, so Otto watches the pointer — even if a text
      // field is focused.
      source = "pointer";
      schedule();
    };

    // Focusing a field only makes it eligible for caret-tracking; it must NOT
    // move the gaze itself, or the composer's autofocus on mount would steal
    // it before the user does anything.
    const onFocusIn = (e: FocusEvent) => {
      if (isTrackedTextField(e.target)) {
        activeField = e.target as HTMLElement;
      }
    };

    const onFocusOut = (e: FocusEvent) => {
      if (e.target === activeField) {
        activeField = null;
        if (source === "caret") {
          source = "pointer";
          schedule();
        }
      }
    };

    // A genuine user-initiated caret move within the focused field (typing,
    // paste/delete, arrow/Home/End navigation, click-to-reposition): pull
    // Otto's gaze to it. Scoped to the active field so input/click elsewhere
    // (a button, another field) while it's focused doesn't hijack the gaze.
    const onCaretMoved = (e: Event) => {
      const t = e.target;
      if (activeField && t instanceof Node && (t === activeField || activeField.contains(t))) {
        source = "caret";
        schedule();
      }
    };

    const CARET_NAV_KEYS = new Set([
      "ArrowLeft",
      "ArrowRight",
      "ArrowUp",
      "ArrowDown",
      "Home",
      "End",
      "PageUp",
      "PageDown",
    ]);
    // Read after the key so selectionStart reflects the new caret position.
    const onKeyUp = (e: KeyboardEvent) => {
      if (CARET_NAV_KEYS.has(e.key)) onCaretMoved(e);
    };

    // Keep the caret in view as its field scrolls or the layout shifts, but
    // only while Otto is actually watching the caret.
    const onCaretMaybeShifted = () => {
      if (source === "caret" && activeField) schedule();
    };

    // The composer autofocuses before this effect attaches its focusin
    // listener, so that first focusin is missed. Adopt an already-focused
    // field here so the first keystroke tracks the caret. Deliberately does
    // NOT set `source` or schedule — the pupils stay centered until the user
    // actually moves the mouse or the caret.
    if (isTrackedTextField(document.activeElement)) {
      activeField = document.activeElement;
    }

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("focusin", onFocusIn);
    window.addEventListener("focusout", onFocusOut);
    document.addEventListener("input", onCaretMoved, true);
    document.addEventListener("keyup", onKeyUp, true);
    document.addEventListener("click", onCaretMoved, true);
    window.addEventListener("scroll", onCaretMaybeShifted, true);
    window.addEventListener("resize", onCaretMaybeShifted);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("focusin", onFocusIn);
      window.removeEventListener("focusout", onFocusOut);
      document.removeEventListener("input", onCaretMoved, true);
      document.removeEventListener("keyup", onKeyUp, true);
      document.removeEventListener("click", onCaretMoved, true);
      window.removeEventListener("scroll", onCaretMaybeShifted, true);
      window.removeEventListener("resize", onCaretMaybeShifted);
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <OttoIcon
      ref={svgRef}
      className={className}
      // The hero mascot is meaningful (not decorative), so OttoIcon's
      // decorative aria-hidden default is overridden with image semantics.
      role="img"
      aria-label="Omnigent"
      aria-hidden={false}
    />
  );
}
