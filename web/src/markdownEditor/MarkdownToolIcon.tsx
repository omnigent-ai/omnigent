// The Markdown toolbar's glyphs: B and I as letters, the rest as thin 16px line icons.

import type { ReactNode } from "react";

import type { MarkdownFormat } from "./markdownFormatting";

const PATHS: Partial<Record<MarkdownFormat, ReactNode>> = {
  heading: (
    <>
      <path d="M3 3v10M10 3v10M3 8h7" />
      <path d="M13 6v7" />
    </>
  ),
  code: (
    <>
      <path d="m5.5 4-4 4 4 4" />
      <path d="m10.5 4 4 4-4 4" />
    </>
  ),
  link: (
    <>
      <path d="M6.5 10.5 5 12a2.1 2.1 0 0 1-3-3l2-2a2.1 2.1 0 0 1 3 0" />
      <path d="m9.5 5.5 1.5-1.5a2.1 2.1 0 0 1 3 3l-2 2a2.1 2.1 0 0 1-3 0" />
      <path d="m6 10 4-4" />
    </>
  ),
  quote: (
    <>
      <path d="M3 3v10" />
      <path d="M6.5 5h6M6.5 8h5M6.5 11h4" />
    </>
  ),
  "unordered-list": (
    <>
      <circle cx="2.5" cy="4" r=".75" fill="currentColor" stroke="none" />
      <circle cx="2.5" cy="8" r=".75" fill="currentColor" stroke="none" />
      <circle cx="2.5" cy="12" r=".75" fill="currentColor" stroke="none" />
      <path d="M6 4h8M6 8h8M6 12h8" />
    </>
  ),
  "ordered-list": (
    <>
      <path d="M1.5 4h2V1.8L2 2.7M1.5 7.3c.4-.5 2-.6 2 .4 0 .8-2 1.7-2 2.5h2M1.5 12.2c.6-.4 2-.3 2 .5 0 .7-.7.9-1.4.9.8 0 1.6.2 1.6.9 0 .9-1.6 1-2.2.5" />
      <path d="M6 3h8M6 8h8M6 13h8" />
    </>
  ),
  "task-list": (
    <>
      <rect x="1.5" y="2" width="4" height="4" rx=".7" />
      <path d="m2.5 4 1 1 2-2" />
      <rect x="1.5" y="10" width="4" height="4" rx=".7" />
      <path d="M8 4h6M8 12h6" />
    </>
  ),
};

export function MarkdownToolIcon({ format }: { format: MarkdownFormat }) {
  if (format === "bold") return <strong className="font-mono text-[11px] leading-none">B</strong>;
  if (format === "italic") return <em className="font-mono text-[11px] leading-none">I</em>;
  return (
    <svg
      aria-hidden
      viewBox="0 0 16 16"
      className="size-3.5 fill-none stroke-current [stroke-linecap:round] [stroke-linejoin:round] [stroke-width:1.35]"
    >
      {PATHS[format]}
    </svg>
  );
}
