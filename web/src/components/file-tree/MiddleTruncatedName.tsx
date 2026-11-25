// A file name that truncates in the middle outside the vendored file tree, split
// as the tree's own rows split it, so a long name keeps its start and extension.

import { splitNameForTruncation } from "@/components/file-tree/vendored";
import { cn } from "@/lib/utils";

export function MiddleTruncatedName({ name, className }: { name: string; className?: string }) {
  const [start, end] = splitNameForTruncation(name);
  if (end === "") return <span className={cn("min-w-0 truncate", className)}>{name}</span>;
  // The start gives way first; the end clips from its own start only once the start is gone.
  return (
    <span className={cn("flex min-w-0", className)}>
      <span className="min-w-0 flex-[0_999999_max-content] truncate">{start}</span>
      <span className="min-w-0 flex-[0_1_max-content] truncate [direction:rtl]">
        <bdi>{end}</bdi>
      </span>
    </span>
  );
}
