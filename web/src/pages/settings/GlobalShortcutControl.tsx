import { useCallback, useEffect, useId, useRef, useState } from "react";

import { ALT_KEY, CTRL_KEY, MOD_KEY, SHIFT_KEY } from "@/components/KeyboardShortcut";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { isMacPlatform } from "@/lib/hotkeys";
import {
  cancelGlobalShortcutRecording,
  type GlobalShortcutSetting,
  getGlobalShortcut,
  recordGlobalShortcut,
  setGlobalShortcut,
  shellRecordsGlobalShortcut,
} from "@/lib/nativeBridge";

const MAC_MODIFIER_GLYPHS: Partial<Record<string, string>> = {
  Control: CTRL_KEY,
  Alt: ALT_KEY,
  Shift: SHIFT_KEY,
  Command: MOD_KEY,
};

const NAMED_KEYS = new Map([
  ["Space", "Space"],
  ["ArrowUp", "Up"],
  ["ArrowDown", "Down"],
  ["ArrowLeft", "Left"],
  ["ArrowRight", "Right"],
  ["Enter", "Enter"],
  ["Tab", "Tab"],
]);

/** Electron's name for a `KeyboardEvent.code`, or undefined if a global shortcut can't use it. */
function acceleratorKeyName(code: string): string | undefined {
  if (/^(Key[A-Z]|Digit[0-9])$/.test(code)) return code.slice(-1);
  if (/^F(1|[2-9]|1\d|2[0-4])$/.test(code)) return code;
  return NAMED_KEYS.get(code);
}

/** The accelerator for a keydown, or undefined unless it has Ctrl, Alt, or Cmd and a usable key. */
function acceleratorFromKeyboardEvent(event: KeyboardEvent): string | undefined {
  const key = acceleratorKeyName(event.code);
  if (key === undefined || !(event.ctrlKey || event.altKey || event.metaKey)) return undefined;
  const modifiers: string[] = [];
  if (event.ctrlKey) modifiers.push("Control");
  if (event.altKey) modifiers.push("Alt");
  if (event.shiftKey) modifiers.push("Shift");
  if (event.metaKey) modifiers.push(isMacPlatform() ? "Command" : "Super");
  return [...modifiers, key].join("+");
}

/** The chord as the user reads it: ⌃⌥⇧⌘ glyphs on macOS, `Ctrl+Alt+Space` elsewhere. */
function formatAccelerator(accelerator: string): string {
  const parts = accelerator.split("+");
  if (isMacPlatform()) return parts.map((part) => MAC_MODIFIER_GLYPHS[part] ?? part).join("");
  return parts.map((part) => (part === "Control" ? "Ctrl" : part)).join("+");
}

/**
 * Opt-in shortcut that brings the desktop app to the front from any app. The
 * shell registers it; this control reads and saves the setting.
 */
export function GlobalShortcutControl() {
  const [setting, setSetting] = useState<GlobalShortcutSetting | null>(null);
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const labelId = useId();
  const descriptionId = useId();

  useEffect(() => {
    void getGlobalShortcut().then((saved) => {
      setSetting(saved);
      // On, but the shell couldn't register it at launch.
      if (saved?.error) setError(saved.error);
    });
  }, []);

  const save = useCallback(async (next: GlobalShortcutSetting) => {
    const result = await setGlobalShortcut(next);
    if (result.ok) {
      setSetting(next);
      setError(null);
    } else {
      setError(result.error ?? "Couldn't save the shortcut.");
    }
  }, []);

  // The shell recording in flight, if any. One runs at a time, so a second click can't
  // leave a hidden one; a cancelled or unmounted one's late result is dropped.
  const shellRecordingRef = useRef<number | null>(null);
  const nextRecordingRef = useRef(0);
  const endShellRecording = useCallback(() => {
    if (shellRecordingRef.current === null) return;
    shellRecordingRef.current = null;
    cancelGlobalShortcutRecording();
  }, []);
  useEffect(() => endShellRecording, [endShellRecording]);

  const startRecording = () => {
    setRecording(true);
    if (!shellRecordsGlobalShortcut() || setting === null || shellRecordingRef.current !== null) {
      return;
    }
    const id = ++nextRecordingRef.current;
    shellRecordingRef.current = id;
    // The shell takes the chord before the app's own shortcuts (Cmd+K, Ctrl+Tab) and menu run.
    void recordGlobalShortcut().then((accelerator) => {
      if (shellRecordingRef.current !== id) return;
      shellRecordingRef.current = null;
      setRecording(false);
      if (accelerator !== null) void save({ enabled: setting.enabled, accelerator });
    });
  };
  const stopRecording = () => {
    setRecording(false);
    endShellRecording();
  };

  // Shells that can't record listen on window here instead, so it works wherever focus is.
  useEffect(() => {
    if (!recording || setting === null || shellRecordsGlobalShortcut()) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const accelerator = acceleratorFromKeyboardEvent(event);
      // Other keys pass through, so Tab still moves focus and cancels the recording.
      if (event.key !== "Escape" && accelerator === undefined) return;
      event.preventDefault();
      event.stopPropagation();
      setRecording(false);
      if (accelerator !== undefined) void save({ enabled: setting.enabled, accelerator });
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [recording, setting, save]);

  if (setting === null) return null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-6">
        <SettingsLabel
          label="Show Omnigent from anywhere"
          labelId={labelId}
          descriptionId={descriptionId}
          className="flex-1"
          description="Brings Omnigent to the front from any app."
          descriptionClassName="text-ui"
        />
        <div className="flex shrink-0 items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            aria-describedby={descriptionId}
            data-testid="global-shortcut-recorder"
            onClick={startRecording}
            onBlur={stopRecording}
          >
            {recording ? "Press a shortcut…" : formatAccelerator(setting.accelerator)}
          </Button>
          <Switch
            aria-labelledby={labelId}
            aria-describedby={descriptionId}
            checked={setting.enabled}
            onCheckedChange={(enabled) => void save({ enabled, accelerator: setting.accelerator })}
            data-testid="global-shortcut-toggle"
            componentId="settings.general.global_shortcut"
          />
        </div>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
