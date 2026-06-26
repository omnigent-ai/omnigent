import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { McpElicitationForm, schemaIsRenderable } from "./McpElicitationForm";

afterEach(() => {
  cleanup();
});

describe("schemaIsRenderable", () => {
  it("accepts a flat primitive schema", () => {
    expect(
      schemaIsRenderable({
        type: "object",
        properties: { name: { type: "string" }, count: { type: "integer" } },
      }),
    ).toBe(true);
  });

  it("rejects an empty / property-less schema (→ binary fallback)", () => {
    expect(schemaIsRenderable({})).toBe(false);
    expect(schemaIsRenderable({ type: "object", properties: {} })).toBe(false);
  });

  it("rejects a nested object or array property (→ binary fallback)", () => {
    expect(
      schemaIsRenderable({ type: "object", properties: { nested: { type: "object" } } }),
    ).toBe(false);
    expect(
      schemaIsRenderable({ type: "object", properties: { items: { type: "array" } } }),
    ).toBe(false);
  });
});

describe("McpElicitationForm", () => {
  const schema = {
    type: "object",
    properties: {
      approve: { type: "boolean", title: "Approve" },
      name: { type: "string", title: "Name" },
    },
    required: ["name"],
  };

  it("gates submit on required fields and submits typed content", () => {
    const onSubmit = vi.fn();
    render(
      <McpElicitationForm
        requestedSchema={schema}
        onSubmit={onSubmit}
        onDecline={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const submit = screen.getByTestId("mcp-elicitation-submit") as HTMLButtonElement;
    // ``name`` is required and empty → submit disabled.
    expect(submit.disabled).toBe(true);

    fireEvent.change(screen.getByTestId("mcp-elicit-field-name"), {
      target: { value: "blue" },
    });
    expect(submit.disabled).toBe(false);

    fireEvent.click(submit);
    // Boolean fields are always included (default false); the untouched
    // ``approve`` switch submits false, ``name`` carries the typed value.
    expect(onSubmit).toHaveBeenCalledWith({ approve: false, name: "blue" });
  });

  it("coerces number fields and applies schema defaults", () => {
    const onSubmit = vi.fn();
    render(
      <McpElicitationForm
        requestedSchema={{
          type: "object",
          properties: {
            flag: { type: "boolean", default: true },
            qty: { type: "integer", title: "Qty" },
          },
          required: ["qty"],
        }}
        onSubmit={onSubmit}
        onDecline={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByTestId("mcp-elicit-field-qty"), { target: { value: "7" } });
    fireEvent.click(screen.getByTestId("mcp-elicitation-submit"));
    // ``flag`` honors its default (true); ``qty`` is submitted as a number.
    expect(onSubmit).toHaveBeenCalledWith({ flag: true, qty: 7 });
  });

  it("calls onDecline and onCancel for the negative actions", () => {
    const onDecline = vi.fn();
    const onCancel = vi.fn();
    render(
      <McpElicitationForm
        requestedSchema={schema}
        onSubmit={vi.fn()}
        onDecline={onDecline}
        onCancel={onCancel}
      />,
    );

    fireEvent.click(screen.getByTestId("mcp-elicitation-decline"));
    expect(onDecline).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("mcp-elicitation-cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
