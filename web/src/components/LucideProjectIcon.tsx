// A project's lucide icon from the code-split catalog. The fallback (the
// project's folder glyph) shows while the catalog loads and for an unknown name.

import { lazy, Suspense } from "react";
import type { LucideIcon, LucideProps } from "lucide-react";

type LucideProjectIconProps = LucideProps & { fallback: LucideIcon };

const LucideIconByName = lazy(async () => {
  const { lucideIcon } = await import("@/lib/lucideIconCatalog");
  return {
    default: function LoadedLucideIcon({
      iconName,
      fallback: Fallback,
      ...props
    }: LucideProjectIconProps & { iconName: string }) {
      const Icon = lucideIcon(iconName);
      return Icon ? <Icon data-lucide-icon={iconName} {...props} /> : <Fallback {...props} />;
    },
  };
});

export function LucideProjectIcon({
  iconName,
  fallback: Fallback,
  ...props
}: LucideProjectIconProps & { iconName: string | null | undefined }) {
  if (!iconName) return <Fallback {...props} />;
  return (
    <Suspense fallback={<Fallback {...props} />}>
      <LucideIconByName iconName={iconName} fallback={Fallback} {...props} />
    </Suspense>
  );
}
