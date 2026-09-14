import { useCallback, useEffect, useRef, useState } from "react";
import {
  isMacKeyboardPlatform,
  parseKeybinding,
  serializeKeybinding,
  useSuspendKeybindingDispatch,
  type KeyModifier,
  type KeyStroke,
} from "@/actions";
import { Button } from "@/components/ui/button";

const MODIFIER_KEYS = new Set(["Alt", "AltGraph", "Control", "Meta", "Shift"]);

function strokeFromEvent(
  event: KeyboardEvent | React.KeyboardEvent,
  preferPhysical: boolean,
): KeyStroke | null {
  if (MODIFIER_KEYS.has(event.key) || event.repeat) return null;
  const isMac = isMacKeyboardPlatform();
  const modifiers: KeyModifier[] = [];
  if (isMac && event.metaKey && !event.ctrlKey) modifiers.push("mod");
  else if (!isMac && event.ctrlKey && !event.metaKey) modifiers.push("mod");
  else {
    if (event.ctrlKey) modifiers.push("ctrl");
    if (event.metaKey) modifiers.push("meta");
  }
  if (event.altKey) modifiers.push("alt");
  if (event.shiftKey) modifiers.push("shift");
  // Alt often rewrites event.key; physical codes keep shortcuts stable across layouts.
  const usePhysical =
    event.code.length > 0 &&
    (preferPhysical || event.altKey || event.key === "[" || event.key === "]");
  const stroke: KeyStroke = {
    modifiers,
    key: usePhysical ? { kind: "code", value: event.code } : { kind: "key", value: event.key },
  };
  try {
    return parseKeybinding(serializeKeybinding([stroke]))[0] ?? null;
  } catch {
    return null;
  }
}

export function KeybindingRecorder({
  onComplete,
  onCancel,
  label = "Record binding",
  preferPhysical = false,
  autoStart = false,
}: {
  onComplete: (sequence: string) => void;
  onCancel: () => void;
  label?: string;
  preferPhysical?: boolean;
  autoStart?: boolean;
}) {
  const [recording, setRecording] = useState(autoStart);
  const [unsupportedKey, setUnsupportedKey] = useState(false);
  const targetRef = useRef<HTMLDivElement>(null);
  useSuspendKeybindingDispatch(recording);

  const cancel = useCallback(() => {
    setRecording(false);
    setUnsupportedKey(false);
    onCancel();
  }, [onCancel]);

  useEffect(() => {
    if (recording) targetRef.current?.focus();
  }, [recording]);

  useEffect(() => {
    if (!recording) return;
    const onVisibility = () => {
      if (document.visibilityState !== "visible") cancel();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [cancel, recording]);

  if (!recording) {
    return (
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => {
          setUnsupportedKey(false);
          setRecording(true);
        }}
      >
        {label}
      </Button>
    );
  }

  return (
    <div
      ref={targetRef}
      role="application"
      tabIndex={0}
      data-testid="keybinding-recorder"
      aria-label="Keybinding recorder"
      aria-describedby="keybinding-recorder-instructions"
      className="flex min-w-64 flex-wrap items-center gap-2 rounded-md border border-primary bg-primary/5 p-2 outline-none ring-2 ring-primary/30"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) cancel();
      }}
      onKeyDownCapture={(event) => {
        if (
          event.nativeEvent.isComposing ||
          event.keyCode === 229 ||
          event.getModifierState("AltGraph")
        ) {
          setUnsupportedKey(false);
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          cancel();
          return;
        }
        if (
          (event.key === "Backspace" || event.key === "Delete") &&
          !event.ctrlKey &&
          !event.metaKey &&
          !event.altKey &&
          !event.shiftKey
        ) {
          event.preventDefault();
          event.stopPropagation();
          setUnsupportedKey(false);
          return;
        }
        const stroke = strokeFromEvent(event, preferPhysical);
        if (!stroke) {
          if (!MODIFIER_KEYS.has(event.key) && !event.repeat) setUnsupportedKey(true);
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        setUnsupportedKey(false);
        setRecording(false);
        onComplete(serializeKeybinding([stroke]));
      }}
    >
      <span id="keybinding-recorder-instructions" className="sr-only">
        Press one key combination. Escape cancels. Backspace or Delete clears the pending input.
      </span>
      <span aria-live="polite" className="text-sm font-medium">
        Press one key combination…
      </span>
      {unsupportedKey && (
        <span role="alert" className="text-sm text-destructive">
          That key cannot be recorded on this device.
        </span>
      )}
    </div>
  );
}
