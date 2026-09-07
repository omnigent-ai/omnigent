// Bottom-of-screen footer for the landing screen: app/community links + a
// "Maintained by Databricks" note, mirroring the design. Links open in the
// user's real browser (window.open on the file:// page is routed out by the
// shell's popup policy, same as the Cloud-docs button).

import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { AndroidGlyph, AppleGlyph, DiscordGlyph } from "@/pages/onboarding/brandGlyphs";

const LINKS: { key: string; label: string; icon: ReactNode; href: string }[] = [
  {
    key: "ios",
    label: "iOS App",
    icon: <AppleGlyph />,
    href: "https://omnigent.ai/docs/interact/mobile#ios-app",
  },
  {
    key: "android",
    label: "Android App",
    icon: <AndroidGlyph />,
    href: "https://omnigent.ai/docs/interact/mobile#android-app",
  },
  {
    key: "github",
    label: "GitHub",
    icon: <GithubMono size={16} />,
    href: "https://github.com/omnigent-ai/omnigent",
  },
  {
    key: "discord",
    label: "Join Discord",
    icon: <DiscordGlyph />,
    href: "https://discord.gg/omnigent",
  },
];

export function LandingFooter() {
  return (
    <div className="fixed inset-x-0 bottom-4 flex flex-wrap items-center justify-center gap-1 text-muted-foreground">
      {LINKS.map((link) => (
        <Button
          key={link.key}
          variant="ghost"
          size="sm"
          className="whitespace-nowrap"
          onClick={() => window.open(link.href, "_blank", "noopener")}
        >
          {link.icon}
          {link.label}
        </Button>
      ))}
      <span aria-hidden className="text-muted-foreground">
        •
      </span>
      <span className="whitespace-nowrap px-2 text-base text-muted-foreground">
        Maintained by Databricks
      </span>
    </div>
  );
}
