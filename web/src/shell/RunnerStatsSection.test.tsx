import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Host, HostStats } from "@/hooks/useHosts";
import { RunnerStatsSection } from "./RunnerStatsSection";

const GIB = 1024 ** 3;
const NOW_MS = Date.UTC(2026, 8, 29, 12, 0, 0);
const NOW_S = NOW_MS / 1000;

const FULL_STATS: HostStats = {
  reported_at: NOW_S - 8,
  cpu_percent: 48,
  memory_total_bytes: 16 * GIB,
  memory_used_bytes: 11.2 * GIB,
  disk_total_bytes: 494 * GIB,
  disk_free_bytes: 182 * GIB,
  net_rx_bytes_per_s: 2.4 * 1024 ** 2,
  net_tx_bytes_per_s: 310 * 1024,
};

function makeHost(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "h1",
    name: "bryan-mbp",
    owner: "bryan",
    status: "online",
    stats: FULL_STATS,
    ...overrides,
  };
}

function meter(label: string): HTMLElement {
  return screen.getByRole("meter", { name: `${label} used` });
}

/** The painted fill width, e.g. 63.2 for `width: 63.2%`. */
function fillPercent(label: string): number {
  const fill = meter(label).firstElementChild as HTMLElement;
  return Number.parseFloat(fill.style.width);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW_MS);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("RunnerStatsSection", () => {
  it("renders the approved layout for an online host", () => {
    render(<RunnerStatsSection host={makeHost()} />);

    expect(screen.getByTestId("runner-stats-header")).toHaveTextContent(
      /^Runner\s*bryan-mbp\s*· online · 8s ago$/,
    );
    const section = screen.getByTestId("runner-stats-section");
    const labels = ["CPU", "Memory", "Disk", "Network"];
    const text = section.textContent ?? "";
    const positions = labels.map((label) => text.indexOf(label));
    expect(positions.every((p) => p >= 0)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);

    expect(meter("CPU")).toHaveAttribute("aria-valuenow", "48");
    expect(section).toHaveTextContent("48%");
    expect(meter("Memory")).toHaveAttribute("aria-valuenow", "70");
    expect(section).toHaveTextContent("11.2 / 16 GB");
    // Disk fills by the USED fraction: (494 - 182) / 494, not the free space.
    expect(meter("Disk")).toHaveAttribute("aria-valuenow", "63");
    expect(fillPercent("Disk")).toBeCloseTo(63.16, 1);
    expect(fillPercent("CPU")).toBe(48);
    expect(section).toHaveTextContent("182 GB free / 494 GB");
    // Assistive tech hears each meter's visible value and words, not arrows.
    expect(meter("CPU")).toHaveAttribute("aria-valuetext", "48%");
    expect(meter("Memory")).toHaveAttribute("aria-valuetext", "11.2 / 16 GB");
    expect(meter("Disk")).toHaveAttribute("aria-valuetext", "182 GB free / 494 GB");
    expect(section).toHaveTextContent("↓ download 2.4 MB/s");
    expect(section).toHaveTextContent("↑ upload 310 KB/s");
    expect(screen.getByText("↓")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("↑")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("download")).toHaveClass("sr-only");
    expect(screen.getByText("upload")).toHaveClass("sr-only");
  });

  it.each([
    [79.9, "normal"],
    [80, "warn"],
    [94.9, "warn"],
    [95, "critical"],
  ])("tones a meter at %s%% used as %s", (used, tone) => {
    render(<RunnerStatsSection host={makeHost({ stats: { ...FULL_STATS, cpu_percent: used } })} />);
    expect(meter("CPU")).toHaveAttribute("data-tone", tone);
  });

  it("tones memory and disk by their used fraction", () => {
    const stats: HostStats = {
      ...FULL_STATS,
      memory_used_bytes: 13 * GIB,
      disk_free_bytes: 20 * GIB,
    };
    render(<RunnerStatsSection host={makeHost({ stats })} />);
    expect(meter("Memory")).toHaveAttribute("data-tone", "warn");
    expect(meter("Disk")).toHaveAttribute("data-tone", "critical");
  });

  it("shows only the header with last-seen age for an offline host", () => {
    // What the server sends once a host disconnects: its last-seen, no readings.
    const host = makeHost({ status: "offline", stats: { reported_at: NOW_S - 300 } });
    render(<RunnerStatsSection host={host} />);

    expect(screen.getByTestId("runner-stats-header")).toHaveTextContent(
      /^Runner\s*bryan-mbp\s*· offline · last seen 5m ago$/,
    );
    expect(screen.queryAllByRole("meter")).toHaveLength(0);
    expect(screen.getByTestId("runner-stats-section")).not.toHaveTextContent("Network");
  });

  it("hides meters for an offline host that still holds readings", () => {
    // A crashed host is offline before its tunnel times out and drops the readings.
    render(<RunnerStatsSection host={makeHost({ status: "offline" })} />);
    expect(screen.getByTestId("runner-stats-header")).toHaveTextContent("· offline · last seen");
    expect(screen.queryAllByRole("meter")).toHaveLength(0);
  });

  it.each([
    ["null", null],
    ["absent", undefined],
  ])("renders nothing when the host reports no stats (%s)", (_name, stats) => {
    const { container } = render(<RunnerStatsSection host={makeHost({ stats })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("omits rows the snapshot lacks", () => {
    const stats: HostStats = {
      reported_at: NOW_S - 2,
      memory_total_bytes: 8 * GIB,
      memory_used_bytes: 2 * GIB,
    };
    render(<RunnerStatsSection host={makeHost({ stats })} />);

    expect(screen.getAllByRole("meter")).toEqual([meter("Memory")]);
    const section = screen.getByTestId("runner-stats-section");
    expect(section).not.toHaveTextContent("CPU");
    expect(section).not.toHaveTextContent("Disk");
    expect(section).not.toHaveTextContent("Network");
  });

  it("switches the age to minutes past the first minute", () => {
    render(
      <RunnerStatsSection
        host={makeHost({ stats: { ...FULL_STATS, reported_at: NOW_S - 125 } })}
      />,
    );
    expect(screen.getByTestId("runner-stats-header")).toHaveTextContent("· online · 2m ago");
  });

  it("clamps the age to 0s when the viewer's clock runs behind the server's", () => {
    render(
      <RunnerStatsSection host={makeHost({ stats: { ...FULL_STATS, reported_at: NOW_S + 20 } })} />,
    );
    expect(screen.getByTestId("runner-stats-header")).toHaveTextContent("· online · 0s ago");
  });

  it("uses a caller-supplied label in the header", () => {
    render(<RunnerStatsSection host={makeHost()} label="Modal sandbox" />);
    expect(screen.getByTestId("runner-stats-header")).toHaveTextContent(
      /^Runner\s*Modal sandbox\s*·/,
    );
  });
});
