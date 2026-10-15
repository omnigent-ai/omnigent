import claudeCodeLogo from "@/assets/claude-code-logo.svg";
import { iconForAgent } from "@/components/AgentCard";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";
import { cn } from "@/lib/utils";

const HARNESS_ICONS: Record<string, { src: string; invertInDark: boolean; className?: string }> = {
  claude: {
    src: claudeCodeLogo,
    invertInDark: false,
    className: "-translate-y-[0.5px]",
  },
  cursor: {
    src: "data:image/svg+xml,%3csvg%20fill='currentColor'%20fill-rule='evenodd'%20height='1em'%20style='flex:none;line-height:1'%20viewBox='0%200%2024%2024'%20width='1em'%20xmlns='http://www.w3.org/2000/svg'%3e%3ctitle%3eCursor%3c/title%3e%3cpath%20d='M22.106%205.68L12.5.135a.998.998%200%2000-.998%200L1.893%205.68a.84.84%200%2000-.419.726v11.186c0%20.3.16.577.42.727l9.607%205.547a.999.999%200%2000.998%200l9.608-5.547a.84.84%200%2000.42-.727V6.407a.84.84%200%2000-.42-.726zm-.603%201.176L12.228%2022.92c-.063.108-.228.064-.228-.061V12.34a.59.59%200%2000-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264%200%20.428.286.296.514z'%3e%3c/path%3e%3c/svg%3e",
    invertInDark: true,
  },
  codex: {
    src: "data:image/svg+xml,%3csvg%20fill='none'%20fill-rule='evenodd'%20height='1em'%20style='flex:none;line-height:1'%20viewBox='0%200%2024%2024'%20width='1em'%20xmlns='http://www.w3.org/2000/svg'%3e%3ctitle%3eCodex%3c/title%3e%3cpath%20clip-rule='evenodd'%20d='M8.086.457a6.105%206.105%200%20013.046-.415c1.333.153%202.521.72%203.564%201.7a.117.117%200%2000.107.029c1.408-.346%202.762-.224%204.061.366l.063.03.154.076c1.357.703%202.33%201.77%202.918%203.198.278.679.418%201.388.421%202.126a5.655%205.655%200%2001-.18%201.631.167.167%200%2000.04.155%205.982%205.982%200%20011.578%202.891c.385%201.901-.01%203.615-1.183%205.14l-.182.22a6.063%206.063%200%2001-2.934%201.851.162.162%200%2000-.108.102c-.255.736-.511%201.364-.987%201.992-1.199%201.582-2.962%202.462-4.948%202.451-1.583-.008-2.986-.587-4.21-1.736a.145.145%200%2000-.14-.032c-.518.167-1.04.191-1.604.185a5.924%205.924%200%2001-2.595-.622%206.058%206.058%200%2001-2.146-1.781c-.203-.269-.404-.522-.551-.821a7.74%207.74%200%2001-.495-1.283%206.11%206.11%200%2001-.017-3.064.166.166%200%2000.008-.074.115.115%200%2000-.037-.064%205.958%205.958%200%2001-1.38-2.202%205.196%205.196%200%2001-.333-1.589%206.915%206.915%200%2001.188-2.132c.45-1.484%201.309-2.648%202.577-3.493.282-.188.55-.334.802-.438.286-.12.573-.22.861-.304a.129.129%200%2000.087-.087A6.016%206.016%200%20015.635%202.31C6.315%201.464%207.132.846%208.086.457zm-.804%207.85a.848.848%200%2000-1.473.842l1.694%202.965-1.688%202.848a.849.849%200%20001.46.864l1.94-3.272a.849.849%200%2000.007-.854l-1.94-3.393zm5.446%206.24a.849.849%200%20000%201.695h4.848a.849.849%200%20000-1.696h-4.848z'%20fill='url(%23codex-gradient)'%3e%3c/path%3e%3cdefs%3e%3clinearGradient%20gradientUnits='userSpaceOnUse'%20id='codex-gradient'%20x1='12'%20x2='12'%20y1='0'%20y2='24'%3e%3cstop%20stop-color='%23B1A7FF'%3e%3c/stop%3e%3cstop%20offset='.5'%20stop-color='%237A9DFF'%3e%3c/stop%3e%3cstop%20offset='1'%20stop-color='%233941FF'%3e%3c/stop%3e%3c/linearGradient%3e%3c/defs%3e%3c/svg%3e",
    invertInDark: false,
  },
  opencode: {
    src: "data:image/svg+xml,%3csvg%20fill='currentColor'%20fill-rule='evenodd'%20height='1em'%20style='flex:none;line-height:1'%20viewBox='0%200%2024%2024'%20width='1em'%20xmlns='http://www.w3.org/2000/svg'%3e%3ctitle%3eopencode%3c/title%3e%3cpath%20d='M16%206H8v12h8V6zm4%2016H4V2h16v20z'%3e%3c/path%3e%3c/svg%3e",
    invertInDark: true,
  },
  pi: {
    src: "data:image/svg+xml,%3csvg%20fill='currentColor'%20fill-rule='evenodd'%20height='1em'%20style='flex:none;line-height:1'%20viewBox='0%200%2024%2024'%20width='1em'%20xmlns='http://www.w3.org/2000/svg'%3e%3ctitle%3ePi%3c/title%3e%3cpath%20clip-rule='evenodd'%20d='M1%201h16.5v11H12v5.5H6.5V23H1V1zm5.5%205.5V12H12V6.5H6.5z'%3e%3c/path%3e%3cpath%20d='M17.5%2012H23v11h-5.5V12z'%3e%3c/path%3e%3c/svg%3e",
    invertInDark: true,
  },
};

export function ComposerAgentIcon({
  agent,
  className,
}: {
  agent: Pick<AvailableAgent, "name" | "harness">;
  className?: string;
}) {
  if (agent.name === "polly" || agent.name === "debby") {
    return (
      <svg viewBox="0 0 16 16" className={cn("size-4 shrink-0", className)} aria-hidden="true">
        <path
          fill="#FF3621"
          d="M14.9371 6.58407L7.899 10.308L0.362478 6.3292L0 6.51327V9.40177L7.899 13.5646L14.9371 9.85487V11.3841L7.899 15.108L0.362478 11.1292L0 11.3133V11.8088L7.899 15.9717L15.7829 11.8088V8.92035L15.4204 8.73628L7.899 12.7009L0.845781 8.99115V7.46195L7.899 11.1717L15.7829 7.00885V4.16283L15.3902 3.95044L7.899 7.90089L1.20826 4.38938L7.899 0.863717L13.3966 3.76637L13.8799 3.5115V3.15752L7.899 0L0 4.16283V4.61593L7.899 8.77876L14.9371 5.05487V6.58407Z"
        />
      </svg>
    );
  }
  const nativeAgent = nativeCodingAgentForAvailableAgent(agent);
  const product = nativeAgent ? HARNESS_ICONS[nativeAgent.iconKind] : undefined;
  const FallbackIcon = iconForAgent(agent);
  return product ? (
    <img
      src={product.src}
      alt=""
      aria-hidden="true"
      data-harness-icon={nativeAgent?.iconKind}
      className={cn(
        "size-4 shrink-0 object-contain",
        product.className,
        product.invertInDark && "dark:invert",
        className,
      )}
    />
  ) : (
    <FallbackIcon
      className={cn("size-4 shrink-0", className)}
      aria-hidden="true"
      data-harness-icon={nativeAgent?.iconKind}
    />
  );
}
