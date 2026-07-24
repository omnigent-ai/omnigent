import type { SVGProps } from "react";

export function ArtifactIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <path d="M12 22a1 1 0 0 1 0-20 10 9 0 0 1 10 9 5 5 0 0 1-5 5h-2.25a1.75 1.75 0 0 0-1.4 2.8l.3.4a1.75 1.75 0 0 1-1.4 2.8z" />
      <circle cx="13.5" cy="6.5" r="1.25" stroke="none" fill="var(--brand-accent)" />
      <circle cx="17.5" cy="10.5" r="1.25" stroke="none" fill="var(--status-green)" />
      <circle cx="6.5" cy="12.5" r="1.25" stroke="none" fill="var(--status-yellow)" />
      <circle cx="8.5" cy="7.5" r="1.25" stroke="none" fill="var(--status-red)" />
    </svg>
  );
}
