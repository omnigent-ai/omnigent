import { useSearchParams } from "@/lib/routing";

const DEBUG_LS_KEY = "debug";

/**
 * Returns true when ``?debug=1`` is present in the current URL **or**
 * ``localStorage.getItem("debug") === "1"``.
 *
 * The localStorage flag is useful for local development so you don't have
 * to keep ``?debug=1`` in every URL. Set it once in the browser console:
 * ``localStorage.setItem("debug", "1")`` and clear with
 * ``localStorage.removeItem("debug")``.
 */
export function useDebugMode(): boolean {
  const [searchParams] = useSearchParams();
  if (searchParams.get("debug") === "1") return true;
  try {
    return localStorage.getItem(DEBUG_LS_KEY) === "1";
  } catch {
    return false;
  }
}
