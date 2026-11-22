import type { ResolvedPathStoreOptions } from "./internal-types";
import type { PathStoreOptions } from "./public-types";

export function resolvePathStoreOptions(options: PathStoreOptions = {}): ResolvedPathStoreOptions {
  return {
    flattenEmptyDirectories: options.flattenEmptyDirectories !== false,
    sort: options.sort ?? "default",
  };
}
