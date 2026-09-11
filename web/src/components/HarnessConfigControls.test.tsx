import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SMART_ROUTING_LABEL } from "@/lib/agentLabels";

import {
  MODEL_MENU_SEARCH_THRESHOLD,
  MODEL_SELECT_DEFAULT,
  MODEL_SELECT_SMART,
  ModelMenuSearch,
  RoutingModelSelect,
  useModelMenuFilter,
} from "./HarnessConfigControls";

const MODELS = [
  { id: "sonnet", label: "Sonnet 5" },
  { id: "opus", label: "Opus 4.10" },
  { id: "haiku", label: "Haiku 4.5" },
];

function openPicker(testId = "model-picker"): HTMLElement {
  fireEvent.click(screen.getByTestId(testId));
  return screen.getByTestId(testId);
}

describe("RoutingModelSelect", () => {
  it("renders the trigger as a combobox with the resolved label", () => {
    render(
      <RoutingModelSelect
        value="opus"
        onValueChange={vi.fn()}
        offerSmartRouting
        testId="model-picker"
        models={MODELS}
      />,
    );

    const trigger = screen.getByRole("combobox");
    expect(trigger).toHaveAttribute("data-testid", "model-picker");
    expect(trigger).toHaveAttribute("aria-label", "Model");
    expect(trigger).toHaveTextContent("Opus 4.10");
  });

  it.each([
    { value: MODEL_SELECT_SMART, expected: SMART_ROUTING_LABEL },
    { value: MODEL_SELECT_DEFAULT, expected: "Default" },
    {
      value: MODEL_SELECT_DEFAULT,
      defaultLabel: "Default (Sonnet 5)",
      expected: "Default (Sonnet 5)",
    },
    { value: "unknown-id", expected: "unknown-id" },
  ])("shows the right trigger label for $value", ({ value, expected, defaultLabel }) => {
    render(
      <RoutingModelSelect
        value={value}
        onValueChange={vi.fn()}
        offerSmartRouting
        testId="model-picker"
        models={MODELS}
        defaultLabel={defaultLabel}
      />,
    );
    expect(screen.getByRole("combobox")).toHaveTextContent(expected);
  });

  it("lists sentinels and models with the correct data attributes", () => {
    render(
      <RoutingModelSelect
        value="opus"
        onValueChange={vi.fn()}
        offerSmartRouting
        testId="model-picker"
        models={MODELS}
        activeModelId="sonnet"
      />,
    );

    openPicker();

    expect(screen.getByRole("option", { name: SMART_ROUTING_LABEL })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Default" })).toBeTruthy();

    const sonnet = screen.getByRole("option", { name: "Sonnet 5" });
    const opus = screen.getByRole("option", { name: "Opus 4.10" });

    expect(sonnet).toHaveAttribute("data-model-id", "sonnet");
    expect(sonnet).toHaveAttribute("data-active", "true");
    expect(opus).toHaveAttribute("data-model-id", "opus");
    expect(opus).not.toHaveAttribute("data-active");
  });

  it("calls onValueChange and closes the dropdown on selection", () => {
    const onValueChange = vi.fn();
    render(
      <RoutingModelSelect
        value={MODEL_SELECT_DEFAULT}
        onValueChange={onValueChange}
        offerSmartRouting
        testId="model-picker"
        models={MODELS}
      />,
    );

    openPicker();
    fireEvent.click(screen.getByRole("option", { name: "Haiku 4.5" }));

    expect(onValueChange).toHaveBeenCalledWith("haiku");
    expect(screen.queryByRole("option", { name: "Haiku 4.5" })).toBeNull();
  });

  it("closes on Escape without selecting", () => {
    const onValueChange = vi.fn();
    render(
      <RoutingModelSelect
        value={MODEL_SELECT_DEFAULT}
        onValueChange={onValueChange}
        offerSmartRouting
        testId="model-picker"
        models={MODELS}
      />,
    );

    openPicker();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    expect(onValueChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("option")).toBeNull();
  });

  it("renders the search input only when the catalog is long", () => {
    const shortModels = Array.from({ length: 15 }, (_, i) => ({
      id: `model-${i}`,
      label: `Model ${i}`,
    }));

    const { rerender } = render(
      <RoutingModelSelect
        value={MODEL_SELECT_DEFAULT}
        onValueChange={vi.fn()}
        offerSmartRouting={false}
        testId="model-picker"
        models={shortModels}
      />,
    );

    openPicker();
    expect(screen.queryByTestId("model-picker-search")).toBeNull();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    const longModels = Array.from({ length: 16 }, (_, i) => ({
      id: `model-${i}`,
      label: `Model ${i}`,
    }));

    rerender(
      <RoutingModelSelect
        value={MODEL_SELECT_DEFAULT}
        onValueChange={vi.fn()}
        offerSmartRouting={false}
        testId="model-picker"
        models={longModels}
      />,
    );

    openPicker();
    expect(screen.getByTestId("model-picker-search")).toBeTruthy();
  });

  it("filters model items but never the sentinels", () => {
    const longModels = [
      { id: "alpha", label: "Alpha One" },
      { id: "beta", label: "Beta Two" },
      ...Array.from({ length: 20 }, (_, i) => ({
        id: `filler-${i}`,
        label: `Filler ${i}`,
      })),
    ];

    render(
      <RoutingModelSelect
        value={MODEL_SELECT_DEFAULT}
        onValueChange={vi.fn()}
        offerSmartRouting
        testId="model-picker"
        models={longModels}
      />,
    );

    openPicker();
    const search = screen.getByTestId("model-picker-search");
    fireEvent.change(search, { target: { value: "alpha" } });

    expect(screen.getByRole("option", { name: "Alpha One" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Beta Two" })).toBeNull();
    expect(screen.getByRole("option", { name: SMART_ROUTING_LABEL })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Default" })).toBeTruthy();
  });

  it("renders caller notes alone on an empty catalog, with no phantom empty state", () => {
    render(
      <RoutingModelSelect
        value={MODEL_SELECT_DEFAULT}
        onValueChange={vi.fn()}
        offerSmartRouting={false}
        testId="model-picker"
        models={[]}
      >
        <div className="px-2.5 py-1 text-sm text-muted-foreground">Loading models…</div>
      </RoutingModelSelect>,
    );

    openPicker();
    expect(screen.getByText("Loading models…")).toBeTruthy();
    expect(screen.queryByText("No models found")).toBeNull();
  });

  it("shows an empty state when the search matches nothing", () => {
    const longModels = Array.from({ length: 20 }, (_, i) => ({
      id: `model-${i}`,
      label: `Model ${i}`,
    }));

    render(
      <RoutingModelSelect
        value={MODEL_SELECT_DEFAULT}
        onValueChange={vi.fn()}
        offerSmartRouting={false}
        testId="model-picker"
        models={longModels}
      />,
    );

    openPicker();
    fireEvent.change(screen.getByTestId("model-picker-search"), {
      target: { value: "zzz" },
    });

    expect(screen.getByText("No models found")).toBeTruthy();
  });
});

/** Renders the menu filter hook through a minimal list, like a menu would. */
function FilterHarness({
  options,
}: {
  options: { id: string; displayName?: string; label?: string }[];
}) {
  const filter = useModelMenuFilter(options);
  return (
    <div>
      {filter.showSearch && <ModelMenuSearch filter={filter} />}
      <ul>
        {filter.filteredOptions.map((option) => (
          <li key={option.id} data-testid={`opt-${option.id}`}>
            {option.displayName ?? option.label ?? option.id}
          </li>
        ))}
      </ul>
      {filter.noResults && <div data-testid="no-results">No models found</div>}
    </div>
  );
}

function longCatalog(count: number, prefix = "model") {
  return Array.from({ length: count }, (_, i) => ({
    id: `${prefix}-${i}`,
    displayName: `${prefix} ${i}`,
  }));
}

describe("useModelMenuFilter", () => {
  it("keeps every option listed until a query is typed", () => {
    render(<FilterHarness options={longCatalog(20)} />);
    expect(screen.getAllByTestId(/^opt-model-/)).toHaveLength(20);
    expect(screen.queryByTestId("no-results")).toBeNull();
  });

  it("requires every whitespace-separated term to match the label or id", () => {
    render(
      <FilterHarness
        options={[
          { id: "glm-5.3-flash", displayName: "GLM 5.3 Flash" },
          { id: "glm-5.3", displayName: "GLM 5.3" },
          { id: "kimi-k3", displayName: "Kimi K3" },
          // Pad past the threshold so the search field renders.
          ...longCatalog(15),
        ]}
      />,
    );
    // Both terms must appear — "glm flash" matches only the flash variant.
    fireEvent.change(screen.getByTestId("composer-agent-models-search"), {
      target: { value: "glm flash" },
    });
    expect(screen.getByTestId("opt-glm-5.3-flash")).toBeTruthy();
    expect(screen.queryByTestId("opt-glm-5.3")).toBeNull();
    expect(screen.queryByTestId("opt-kimi-k3")).toBeNull();
  });

  it("matches against the id when the label does not carry the term", () => {
    render(
      <FilterHarness
        options={[
          { id: "zai/glm-5.3", displayName: "GLM 5.3" },
          { id: "moonshotai/kimi-k3", displayName: "Kimi K3" },
          ...longCatalog(15),
        ]}
      />,
    );
    fireEvent.change(screen.getByTestId("composer-agent-models-search"), {
      target: { value: "moonshotai" },
    });
    expect(screen.queryByTestId("opt-zai/glm-5.3")).toBeNull();
    expect(screen.getByTestId("opt-moonshotai/kimi-k3")).toBeTruthy();
  });

  it("falls back through displayName, label, then id for matching", () => {
    render(
      <FilterHarness
        options={[{ id: "a", label: "Alpha" }, { id: "beta-only-id" }, ...longCatalog(15)]}
      />,
    );
    fireEvent.change(screen.getByTestId("composer-agent-models-search"), {
      target: { value: "beta" },
    });
    expect(screen.getByTestId("opt-beta-only-id")).toBeTruthy();
    expect(screen.queryByTestId("opt-a")).toBeNull();
  });
});

describe("useModelMenuFilter / ModelMenuSearch", () => {
  const baseOptions = [
    { id: "alpha", label: "Alpha One" },
    { id: "beta", label: "Beta Two" },
    ...Array.from({ length: 20 }, (_, i) => ({ id: `filler-${i}`, label: `Filler ${i}` })),
  ];

  it("only shows search for catalogs longer than the threshold", () => {
    const short = renderHook(() =>
      useModelMenuFilter(baseOptions.slice(0, MODEL_MENU_SEARCH_THRESHOLD)),
    );
    expect(short.result.current.showSearch).toBe(false);

    const long = renderHook(() => useModelMenuFilter(baseOptions));
    expect(long.result.current.showSearch).toBe(true);
  });

  it("matches labels case-insensitively", () => {
    const { result } = renderHook(() => useModelMenuFilter(baseOptions));
    act(() => result.current.setQuery("ALPHA one"));
    expect(result.current.filteredOptions).toHaveLength(1);
    expect(result.current.filteredOptions[0].id).toBe("alpha");
  });

  it("matches ids case-insensitively", () => {
    const { result } = renderHook(() => useModelMenuFilter(baseOptions));
    act(() => result.current.setQuery("FILLER-3"));
    expect(result.current.filteredOptions).toHaveLength(1);
    expect(result.current.filteredOptions[0].id).toBe("filler-3");
  });

  it("reports no results when nothing matches", () => {
    const { result } = renderHook(() => useModelMenuFilter(baseOptions));
    act(() => result.current.setQuery("zzz"));
    expect(result.current.noResults).toBe(true);
    expect(result.current.filteredOptions).toHaveLength(0);
  });

  it("clears the query when the caller resets it", () => {
    const { result } = renderHook(() => useModelMenuFilter(baseOptions));
    act(() => result.current.setQuery("alpha"));
    expect(result.current.filteredOptions).toHaveLength(1);
    act(() => result.current.setQuery(""));
    expect(result.current.filteredOptions).toHaveLength(baseOptions.length);
  });

  it("renders the shared search input with the required test id", () => {
    function Wrapper() {
      const filter = useModelMenuFilter(baseOptions);
      return <ModelMenuSearch filter={filter} />;
    }
    render(<Wrapper />);
    const input = screen.getByTestId("composer-agent-models-search");
    expect(input).toHaveAttribute("placeholder", "Search models…");
    expect(input).toHaveAttribute("aria-label", "Search models");
  });

  it("stops keydown propagation on the search input", () => {
    function Wrapper() {
      const filter = useModelMenuFilter(baseOptions);
      return <ModelMenuSearch filter={filter} />;
    }
    const parentKeyDown = vi.fn();
    render(
      <div onKeyDown={parentKeyDown}>
        <Wrapper />
      </div>,
    );
    fireEvent.keyDown(screen.getByTestId("composer-agent-models-search"), {
      key: "ArrowDown",
      code: "ArrowDown",
    });
    expect(parentKeyDown).not.toHaveBeenCalled();
  });
});
