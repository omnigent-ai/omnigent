import { useCallback, useEffect, useRef, useState } from "react";

import { ALT_KEY, CTRL_KEY, MOD_KEY, SHIFT_KEY } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { isMacPlatform } from "@/lib/hotkeys";
import {
  cancelGlobalShortcutRecording,
  type GlobalShortcutChange,
  type GlobalShortcutId,
  type GlobalShortcutSetting,
  getDesktopFeatures,
  getGlobalShortcut,
  recordGlobalShortcut,
  setGlobalShortcut,
  shellRecordsGlobalShortcut,
} from "@/lib/nativeBridge";
import { GLOBAL_SHORTCUT_ENTRIES } from "./search/catalog/general";
import { SettingsEntryLabel, SettingsItem } from "./search/SettingsItem";
import { settingsControlProps, settingsDescriptionId } from "./search/settingsControl";

const RECORDING_PROMPT = "Press a shortcut…";

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

/** Each shortcut's test and component ids; its name and copy come from its catalog entry. */
const SHORTCUT_IDS: Record<GlobalShortcutId, { testIdPrefix: string; componentId: string }> = {
  show: { testIdPrefix: "global-shortcut", componentId: "settings.general.global_shortcut" },
  floatingComposer: {
    testIdPrefix: "floating-composer-shortcut",
    componentId: "settings.general.floating_composer_shortcut",
  },
};

/**
 * A system-wide shortcut: by default the opt-in one that brings the desktop app to the front
 * from any app, or the one that opens the New Agent panel. The shell registers it; this
 * control reads and saves the setting.
 */
export function GlobalShortcutControl({ shortcutId = "show" }: { shortcutId?: GlobalShortcutId }) {
  const entry = GLOBAL_SHORTCUT_ENTRIES.entry(shortcutId);
  const ids = SHORTCUT_IDS[shortcutId];
  const [setting, setSetting] = useState<GlobalShortcutSetting | null>(null);
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // An older shell ignores the id and would read and save "show" instead.
      if (
        shortcutId !== "show" &&
        (await getDesktopFeatures())?.floatingComposerShortcut !== true
      ) {
        return;
      }
      const saved = await getGlobalShortcut(shortcutId);
      if (cancelled) return;
      setSetting(saved);
      // On, but the shell couldn't register it at launch.
      if (saved?.error) setError(saved.error);
    })();
    return () => {
      cancelled = true;
    };
  }, [shortcutId]);

  const save = useCallback(
    async (next: GlobalShortcutChange) => {
      const result = await setGlobalShortcut(next, shortcutId);
      if (result.ok) {
        // Turning off keeps the shell's saved chord, which another window may have changed.
        setSetting(
          (current) => result.setting ?? { accelerator: current?.accelerator ?? "", ...next },
        );
        setError(null);
      } else {
        setError(result.error ?? "Couldn't save the shortcut.");
      }
    },
    [shortcutId],
  );

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
    void recordGlobalShortcut(shortcutId).then((accelerator) => {
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
    <SettingsItem entry={entry} className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-6">
        <SettingsEntryLabel entry={entry} className="flex-1" descriptionClassName="text-ui" />
        <div className="flex shrink-0 items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            aria-describedby={settingsDescriptionId(entry)}
            {...settingsControlProps(entry, "recorder", {
              runtime: recording ? RECORDING_PROMPT : formatAccelerator(setting.accelerator),
            })}
            data-testid={`${ids.testIdPrefix}-recorder`}
            onClick={startRecording}
            onBlur={stopRecording}
          >
            {recording ? RECORDING_PROMPT : formatAccelerator(setting.accelerator)}
          </Button>
          <Switch
            {...settingsControlProps(entry)}
            checked={setting.enabled}
            onCheckedChange={(enabled) =>
              // Turning off names no chord, so a window showing an older one can't save it.
              void save(
                enabled ? { enabled, accelerator: setting.accelerator } : { enabled: false },
              )
            }
            data-testid={`${ids.testIdPrefix}-toggle`}
            componentId={ids.componentId}
          />
        </div>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </SettingsItem>
  );
}
