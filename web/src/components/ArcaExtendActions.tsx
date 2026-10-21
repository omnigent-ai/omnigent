import { useEffect, useRef, useState, type ReactNode } from "react";
import { offersWorkweek, persistDismissalToday } from "@/lib/arcaShutdownWarning";
import {
  extendArca,
  getDesktopFeatures,
  hasArcaExtendBridge,
  type ArcaExtendMode,
} from "@/lib/nativeBridge";

export function useCanExtendArca(): boolean | null {
  const [available, setAvailable] = useState<boolean | null>(() =>
    hasArcaExtendBridge() ? null : false,
  );

  useEffect(() => {
    if (!hasArcaExtendBridge()) return;
    let active = true;
    void getDesktopFeatures().then((features) => {
      if (active) setAvailable(features?.canExtendArca === true);
    });
    return () => {
      active = false;
    };
  }, []);

  return available;
}

export function ArcaExtendActions({
  day,
  onSuccess,
  fallback,
}: {
  day: string;
  onSuccess: () => void;
  fallback: (mode: ArcaExtendMode) => ReactNode;
}) {
  const [pending, setPending] = useState<ArcaExtendMode | null>(null);
  const [error, setError] = useState<{ message: string; mode: ArcaExtendMode } | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  const run = async (mode: ArcaExtendMode) => {
    if (pending) return;
    setPending(mode);
    setError(null);
    const result = await extendArca(mode);
    if (result.ok) persistDismissalToday(new Date());
    if (!mounted.current) return;
    setPending(null);
    if (!result.ok) {
      setError({ message: result.error, mode });
      return;
    }
    const until =
      result.shutdownAt === null
        ? ""
        : ` until ${new Date(result.shutdownAt).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
    setConfirmation(`Arca will keep running${until}`);
    timer.current = setTimeout(onSuccess, 2000);
  };

  if (confirmation) return <p role="status">{confirmation}</p>;

  return (
    <div>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={pending !== null}
          onClick={() => void run("overnight")}
          className="rounded border border-warning/40 px-2 py-0.5 hover:bg-warning/10 disabled:opacity-50"
        >
          {pending === "overnight" ? "Extending Arca…" : "Keep running tonight"}
        </button>
        {offersWorkweek(new Date(`${day}T12:00:00`)) && (
          <button
            type="button"
            disabled={pending !== null}
            onClick={() => void run("workweek")}
            className="rounded border border-warning/40 px-2 py-0.5 hover:bg-warning/10 disabled:opacity-50"
          >
            {pending === "workweek" ? "Extending Arca…" : "Keep running this week"}
          </button>
        )}
      </div>
      {error && (
        <div className="mt-1" role="alert">
          <p className="text-destructive">{error.message}</p>
          <p>Run the command on your laptop instead:</p>
          {fallback(error.mode)}
        </div>
      )}
    </div>
  );
}
