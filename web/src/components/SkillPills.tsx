import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * A compact, left-aligned row of skill pills rendered inline on the
 * new-session composer's first line, beside the prompt text. The whole
 * empty-state affordance (prompt + pills) is hidden once the user starts
 * typing, so the pills are only ever shown over an empty draft.
 *
 * One pill per bundled skill; clicking hands the bare skill name to the
 * caller, which prefills the composer — pills never auto-execute,
 * matching the "/" menu's semantics. Hovering (or focusing) a pill shows
 * a bubble with the skill's name and description, mirroring the "/"
 * menu's detail card.
 *
 * Rendered inside a pointer-events-none overlay, so each pill re-enables
 * pointer events (clicks between pills fall through to the textarea).
 *
 * @param skills Bundled skills to render, e.g. from GET /v1/agents.
 * @param onPick Called with the bare skill name (no slash) on click.
 */
export function SkillPills({
  skills,
  onPick,
}: {
  skills: ReadonlyArray<{ name: string; description: string }>;
  onPick: (name: string) => void;
}) {
  if (skills.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="skill-pills">
      {skills.map((skill) => (
        <Tooltip key={skill.name}>
          <TooltipTrigger asChild>
            <button
              type="button"
              data-testid={`skill-pill-${skill.name}`}
              onClick={() => onPick(skill.name)}
              className="pointer-events-auto rounded-md bg-brand-accent/10 px-2 py-1 text-13 leading-none text-brand-accent transition-colors hover:bg-brand-accent/15"
            >
              /{skill.name}
            </button>
          </TooltipTrigger>
          <TooltipContent side="top" align="start" className="block max-w-80 p-3">
            <p className="text-sm font-semibold text-foreground">/{skill.name}</p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              {skill.description}
            </p>
          </TooltipContent>
        </Tooltip>
      ))}
    </div>
  );
}
