import { useEffect, useRef, useState } from "react";
import { SettingsGroup } from "@/components/SettingsGroup";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  getNativeNotificationSettings,
  setNativeNotificationSettings,
  onNativeNotificationModeChanged,
  previewNativeNotificationSound,
  openNativeNotificationSettings,
  nativeNotify,
  type NativeNotificationSettings,
} from "@/lib/nativeBridge";

export function SettingsNotificationsSection() {
  const [settings, setSettings] = useState<NativeNotificationSettings | null>();
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const revision = useRef(0);

  useEffect(() => {
    const refresh = async () => {
      const request = ++revision.current;
      try {
        const value = await getNativeNotificationSettings();
        if (request === revision.current) setSettings(value);
      } catch {
        if (request === revision.current)
          setError("Could not load notification settings. Reopen this page to try again.");
      }
    };
    void refresh();
    const unsubscribe = onNativeNotificationModeChanged(() => void refresh());
    return () => {
      ++revision.current;
      unsubscribe();
    };
  }, []);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    setStatus("");
    try {
      await action();
    } catch {
      setError("Could not complete that change. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function save(patch: Partial<NativeNotificationSettings>) {
    await run(async () => {
      const value = await setNativeNotificationSettings(patch);
      ++revision.current;
      setSettings(value);
    });
  }

  return (
    <section>
      <h1 className="settings-page-title text-2xl font-semibold">Notifications</h1>
      <p className="mt-1 text-ui text-muted-foreground">
        Get notified when an agent finishes or needs your input.
      </p>
      {error && (
        <p role="alert" className="mt-4 text-sm text-destructive">
          {error}
        </p>
      )}
      {settings === undefined && !error && (
        <p role="status" className="mt-6">
          Loading notification settings…
        </p>
      )}
      {settings === null && (
        <p className="mt-6 text-sm text-muted-foreground">
          These controls require an up-to-date Omnigent desktop app on macOS.
        </p>
      )}
      {settings && (
        <fieldset disabled={busy} className="mt-6 flex flex-col gap-6 disabled:opacity-60">
          <SettingsGroup title="When to notify">
            <div className="flex flex-col gap-4">
              {(
                [
                  [
                    "when-away",
                    "When I’m away from the conversation",
                    "Stay quiet while I’m viewing the active conversation.",
                  ],
                  ["always", "Always", "Notify me even while I’m viewing the active conversation."],
                ] as const
              ).map(([mode, label, description]) => (
                <label key={mode} className="flex cursor-pointer items-start gap-3">
                  <input
                    type="radio"
                    name="notification-mode"
                    className="mt-1 accent-primary"
                    checked={settings.mode === mode}
                    onChange={() => void save({ mode })}
                  />
                  <span>
                    <span className="text-ui font-medium">{label}</span>
                    <span className="mt-1 block text-sm text-muted-foreground">{description}</span>
                  </span>
                </label>
              ))}
            </div>
          </SettingsGroup>
          <SettingsGroup title="Sound" contentClassName="flex flex-col gap-4">
            <div className="flex items-center justify-between gap-4">
              <label htmlFor="notification-sound" className="text-ui font-medium">
                Play notification sound
              </label>
              <Switch
                id="notification-sound"
                checked={settings.soundEnabled}
                onCheckedChange={(soundEnabled) => void save({ soundEnabled })}
              />
            </div>
            <div className="flex items-center gap-3">
              <Select
                value={settings.soundName}
                disabled={busy}
                onValueChange={(soundName) => void save({ soundName })}
              >
                <SelectTrigger aria-label="Notification sound" className="w-48">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {settings.sounds.map((name) => (
                    <SelectItem key={name} value={name}>
                      {name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                onClick={() => void run(() => previewNativeNotificationSound(settings.soundName))}
              >
                Preview sound
              </Button>
            </div>
            <p className="text-sm text-muted-foreground">
              Preview plays even when notification sounds are off.
            </p>
          </SettingsGroup>
          <SettingsGroup title="Try it out" contentClassName="flex flex-col items-start gap-3">
            <Button
              variant="outline"
              onClick={() =>
                void run(async () => {
                  const sent = await nativeNotify({
                    title: "Test notification",
                    body: "This is how Omnigent will notify you.",
                    navigatePath: "/settings/notifications",
                  });
                  if (!sent) throw new Error("Notification unavailable");
                  setStatus(
                    "Test sent. If no alert appeared, check macOS notification settings and Focus.",
                  );
                })
              }
            >
              Send test notification
            </Button>
            <p className="text-sm text-muted-foreground">
              Choose banners or alerts and allow notifications in macOS settings.
            </p>
            <Button
              variant="link"
              className="h-auto p-0"
              onClick={() => void run(openNativeNotificationSettings)}
            >
              Open macOS notification settings
            </Button>
            {status && (
              <p role="status" className="text-sm text-muted-foreground">
                {status}
              </p>
            )}
          </SettingsGroup>
          <p className="text-sm text-muted-foreground">
            Applies to all Omnigent windows on this Mac.
          </p>
        </fieldset>
      )}
    </section>
  );
}
