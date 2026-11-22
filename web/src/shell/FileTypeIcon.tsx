// A file's type icon outside the file trees, such as on a workspace tab: the trees'
// colored glyph where they know the language, else WorkspaceFileIcon's group glyph.

import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { resolveFileIcon, useFileTreeSpriteSheet } from "@/components/file-tree/vendored";
import {
  FILE_ICON_PALETTE,
  FILE_ICON_TOKEN_COLORS,
} from "@/components/file-tree/fileTypeIconPalette";
import { cn } from "@/lib/utils";

import { WorkspaceFileIcon } from "./WorkspaceFileIcon";

/** The trees' icon for `path`, or null where they only have their uncolored default. */
export function treeFileIcon(path: string): { name: string; color: string } | null {
  const icon = resolveFileIcon(path);
  const paletteColor = icon.token ? FILE_ICON_TOKEN_COLORS[icon.token] : undefined;
  if (!paletteColor) return null;
  return { name: icon.name, color: FILE_ICON_PALETTE[paletteColor] };
}

function SpriteIcon({
  icon,
  className,
}: {
  icon: { name: string; color: string };
  className?: string;
}) {
  useFileTreeSpriteSheet();
  const dark = useResolvedThemeMode() === "dark";
  return (
    <svg
      aria-hidden
      data-file-type-icon={icon.name}
      viewBox="0 0 16 16"
      className={cn("size-4 shrink-0", className)}
      // light-dark() resolves against the element's color-scheme, which the app doesn't set globally.
      style={{ color: icon.color, colorScheme: dark ? "dark" : "light" }}
    >
      <use href={`#${icon.name}`} />
    </svg>
  );
}

export function FileTypeIcon({ path, className }: { path: string; className?: string }) {
  const icon = treeFileIcon(path);
  if (!icon) return <WorkspaceFileIcon path={path} className={cn("size-4", className)} />;
  return <SpriteIcon icon={icon} className={className} />;
}
