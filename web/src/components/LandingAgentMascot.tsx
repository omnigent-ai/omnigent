import { OttoEyes } from "@/components/OttoEyes";

export type LandingAgentMascotVariant = "otto" | "willy";

export function landingAgentMascotVariant(
  agentName: string | null | undefined,
): LandingAgentMascotVariant {
  return agentName?.trim().toLowerCase() === "willy" ? "willy" : "otto";
}

export function LandingAgentMascot({ agentName }: { agentName: string | null | undefined }) {
  const variant = landingAgentMascotVariant(agentName);

  if (variant === "otto") {
    return (
      <div
        className="flex h-[88px] w-[108px] shrink-0 items-center justify-center"
        data-testid="new-chat-landing-mascot"
        data-variant={variant}
      >
        <OttoEyes className="h-18 w-auto" />
      </div>
    );
  }

  return (
    <div
      className="relative h-[88px] w-[108px] shrink-0"
      data-testid="new-chat-landing-mascot"
      data-variant={variant}
    >
      <div className="willy-landing-halo absolute inset-1 rounded-full" />
      <span className="willy-landing-orbit willy-landing-orbit--pink absolute top-[26px] left-2 h-[34px] w-[92px] rounded-[50%] border" />
      <span className="willy-landing-orbit willy-landing-orbit--green absolute top-[31px] left-4 h-[26px] w-[77px] rounded-[50%] border" />
      <span className="willy-landing-satellite willy-landing-satellite--pink absolute top-6 right-3 size-1 rounded-full" />
      <span className="willy-landing-satellite willy-landing-satellite--green absolute bottom-[21px] left-4 size-1 rounded-full" />
      <span className="willy-landing-paint-mark absolute right-2 bottom-2 h-1.5 w-7 rounded-full" />
      <OttoEyes
        className="willy-landing-mascot absolute top-5 left-[30px] h-12 w-auto"
        variant="painted"
        ariaLabel="Willy"
      />
      <svg
        viewBox="0 0 56 56"
        className="willy-landing-paintbrush absolute right-1 bottom-1 h-7 w-7"
        aria-hidden="true"
        data-testid="willy-paintbrush"
      >
        <path
          d="M9 47 31 25"
          fill="none"
          stroke="#9A603A"
          strokeLinecap="round"
          strokeWidth="6.5"
        />
        <path
          d="M11 44 29 26"
          fill="none"
          stroke="#D39A6A"
          strokeLinecap="round"
          strokeWidth="1.8"
        />
        <path d="m27 29-6-6 12-12 9 9-12 12Z" className="fill-muted-foreground" />
        <path d="m29 27-4-4 10-10 5 5Z" className="fill-card" opacity="0.72" />
        <path d="m34 13 7-7c4-4 10-5 11-3 2 3-1 10-5 14l-6 6Z" className="fill-brand-accent" />
        <path
          d="m39 10 8-6M42 14l8-7M45 17l6-7"
          fill="none"
          stroke="currentColor"
          strokeLinecap="round"
          strokeWidth="1.4"
          className="text-card"
        />
        <path d="M50 4c3 2 3 6 0 8 1-3 0-5-2-7Z" className="fill-brand-accent" />
        <circle cx="8.5" cy="47.5" r="1.5" className="fill-foreground/45" />
      </svg>
    </div>
  );
}
