import { useSearchParams } from "@/lib/routing";

/**
 * Returns true when ``?debug=1`` is present in the current URL.
 *
 * Used to reveal developer-facing panels (e.g. Execution logs) that are
 * hidden in normal use. Append ``?debug=1`` to any page URL to enable.
 */
export function useDebugMode(): boolean {
  const [searchParams] = useSearchParams();
  return searchParams.get("debug") === "1";
}
