import type { FileTreeResolvedIcon } from "./icons";

export function Icon({ name, token, size = 16 }: FileTreeResolvedIcon & { size?: number }) {
  return (
    <svg
      data-icon-name={token === undefined ? name : "file-tree-icon-file"}
      data-icon-token={token}
      aria-hidden
      viewBox={`0 0 ${size} ${size}`}
      width={size}
      height={size}
    >
      <use href={`#${name}`} />
    </svg>
  );
}
