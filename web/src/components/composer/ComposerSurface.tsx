import type { FormHTMLAttributes, HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

interface ComposerSurfaceCommonProps {
  children: ReactNode;
  className?: string;
  isDragActive?: boolean;
}

function ComposerDropOverlay() {
  return (
    <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-card/80">
      <span className="text-sm font-medium text-ring">Drop files here</span>
    </div>
  );
}

export const COMPOSER_SURFACE_CLASS_NAME =
  "reference-composer-surface relative z-10 flex min-h-[58px] w-full flex-row flex-wrap items-center gap-2 rounded-[16px] border border-border bg-card p-3 transition-[border-color,box-shadow] duration-150";

type ComposerSurfaceProps = ComposerSurfaceCommonProps & HTMLAttributes<HTMLDivElement>;

export function ComposerSurface({
  children,
  className,
  isDragActive = false,
  ...props
}: ComposerSurfaceProps) {
  return (
    <div
      className={cn(
        COMPOSER_SURFACE_CLASS_NAME,
        isDragActive && "ring-2 ring-ring ring-inset",
        className,
      )}
      {...props}
    >
      {isDragActive && <ComposerDropOverlay />}
      {children}
    </div>
  );
}

type ComposerFormSurfaceProps = ComposerSurfaceCommonProps & FormHTMLAttributes<HTMLFormElement>;

export function ComposerFormSurface({
  children,
  className,
  isDragActive = false,
  ...props
}: ComposerFormSurfaceProps) {
  return (
    <form
      className={cn(
        COMPOSER_SURFACE_CLASS_NAME,
        isDragActive && "ring-2 ring-ring ring-inset",
        className,
      )}
      {...props}
    >
      {isDragActive && <ComposerDropOverlay />}
      {children}
    </form>
  );
}
