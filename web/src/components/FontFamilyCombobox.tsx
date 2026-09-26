// Searchable font picker for the Settings → Appearance font controls.
//
// Replaces the old free-text font inputs: lists a font-catalog category (see
// lib/fontCatalog.ts) by label, each option previewed in its own face, with a
// "Default" row and a free-text escape hatch so a custom family the catalog
// doesn't know is still honored. Selecting a catalog option triggers the
// webfont loader (via the caller's preference-apply, and eagerly on open here so
// previews render) so the font actually loads without a local install.

import { useEffect, useMemo, useState } from "react";
import { ChevronsUpDownIcon } from "lucide-react";

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { type FontCategory, FONT_CATALOG_BY_CATEGORY } from "@/lib/fontCatalog";
import { loadFontByFamily } from "@/lib/webFontLoader";
import { cn } from "@/lib/utils";

interface FontFamilyComboboxProps {
  /** Which catalog category to list. */
  category: FontCategory;
  /** Current family; "" means the default (no override). */
  value: string;
  /** Fired with the chosen family ("" for default). */
  onChange: (family: string) => void;
  /** Label for the "no override" row, e.g. "System default" / "Editor default". */
  defaultLabel: string;
  /** Accessible name for the trigger + search. */
  ariaLabel: string;
  /** Base for the control's data-testids (`<testId>-trigger`, `-input`, …). */
  testId: string;
  /** Fallback CSS stack appended to a preview so an unloaded face degrades. */
  previewFallback: string;
}

/** A resolved option row: the family to apply and how to display it. */
interface FontOption {
  /** Family to persist/apply; "" = default. */
  family: string;
  /** Human label shown in the trigger and list. */
  label: string;
}

export function FontFamilyCombobox({
  category,
  value,
  onChange,
  defaultLabel,
  ariaLabel,
  testId,
  previewFallback,
}: FontFamilyComboboxProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");

  // Catalog entries for this role, deduped by family (a family can appear more
  // than once — e.g. IBM Plex Mono in fixedWidth and code). The empty-family
  // "system default" catalog entry is dropped: the Default row below covers it.
  const options = useMemo<FontOption[]>(() => {
    const seen = new Set<string>();
    const rows: FontOption[] = [];
    for (const entry of FONT_CATALOG_BY_CATEGORY[category]) {
      if (!entry.family) continue;
      const key = entry.family.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({ family: entry.family, label: entry.label });
    }
    return rows;
  }, [category]);

  // A stored family this category's dropdown doesn't offer (a locally-installed
  // or previously typed font, or a family that only lives in a DIFFERENT catalog
  // category). Surface it as its own selectable row so the current choice is
  // visible and re-selectable — the backward-compat escape hatch. Membership is
  // decided against THIS category's options only: a cross-category catalog match
  // (e.g. a sans slot holding "Fira Code", a code-only family) must still show
  // as Custom, so we don't use getFontByFamily's cross-category fallback here.
  const customOption = useMemo<FontOption | null>(() => {
    if (!value) return null;
    const key = value.toLowerCase();
    if (options.some((option) => option.family.toLowerCase() === key)) return null;
    return { family: value, label: value };
  }, [value, options]);

  // On open, eagerly load every listed catalog face so its preview renders in
  // its own font rather than the fallback. Fire-and-forget; non-catalog and
  // bundled entries no-op inside the loader.
  useEffect(() => {
    if (!open) return;
    for (const option of options) loadFontByFamily(option.family, category);
  }, [open, options, category]);

  const selectedLabel = value
    ? (customOption?.label ??
      options.find((option) => option.family.toLowerCase() === value.toLowerCase())?.label ??
      value)
    : defaultLabel;

  const select = (family: string) => {
    onChange(family);
    setOpen(false);
    setSearch("");
  };

  // Let the user apply a typed family the list doesn't contain (custom fallback).
  const trimmedSearch = search.trim();
  const hasExactMatch = options.some(
    (option) => option.label.toLowerCase() === trimmedSearch.toLowerCase(),
  );
  const showCustomEntry = trimmedSearch.length > 0 && !hasExactMatch;

  const previewStyle = (family: string) => ({ fontFamily: `"${family}", ${previewFallback}` });

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setSearch("");
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-label={ariaLabel}
          data-testid={`${testId}-trigger`}
          className={cn(
            "flex h-9 w-56 items-center justify-between gap-2 rounded-md border border-input bg-background px-3 text-sm shadow-xs transition-colors outline-none dark:bg-input/30",
            "hover:border-border-strong focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
          )}
        >
          <span className="min-w-0 truncate" style={value ? previewStyle(value) : undefined}>
            {selectedLabel}
          </span>
          <ChevronsUpDownIcon className="size-4 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-0" data-testid={`${testId}-popover`}>
        <Command
          // Search matches labels; keep the typed custom entry visible too.
          filter={(itemValue, query) =>
            itemValue.toLowerCase().includes(query.trim().toLowerCase()) ? 1 : 0
          }
        >
          <CommandInput
            placeholder={`Search ${ariaLabel.toLowerCase()}…`}
            aria-label={ariaLabel}
            data-testid={`${testId}-input`}
            value={search}
            onValueChange={setSearch}
          />
          <CommandList>
            <CommandEmpty>No fonts found.</CommandEmpty>
            <CommandGroup>
              {/* Default (no override). */}
              <CommandItem
                value={defaultLabel}
                data-testid={`${testId}-option-default`}
                data-checked={value === ""}
                onSelect={() => select("")}
              >
                <span className="truncate">{defaultLabel}</span>
              </CommandItem>

              {/* An out-of-catalog stored family, so the current custom value shows. */}
              {customOption && (
                <CommandItem
                  value={customOption.label}
                  data-testid={`${testId}-option-custom-current`}
                  data-checked
                  onSelect={() => select(customOption.family)}
                >
                  <span className="truncate" style={previewStyle(customOption.family)}>
                    {customOption.label}
                  </span>
                  <span className="ml-2 shrink-0 text-xs text-muted-foreground">Custom</span>
                </CommandItem>
              )}

              {options.map((option) => (
                <CommandItem
                  key={option.family}
                  value={option.label}
                  data-testid={`${testId}-option-${option.family}`}
                  data-checked={option.family.toLowerCase() === value.toLowerCase()}
                  onSelect={() => select(option.family)}
                >
                  <span className="truncate" style={previewStyle(option.family)}>
                    {option.label}
                  </span>
                </CommandItem>
              ))}

              {/* Free-text escape hatch: apply whatever family was typed. */}
              {showCustomEntry && (
                <CommandItem
                  value={`__custom__ ${trimmedSearch}`}
                  data-testid={`${testId}-option-custom`}
                  onSelect={() => select(trimmedSearch)}
                >
                  <span className="truncate" style={previewStyle(trimmedSearch)}>
                    Use "{trimmedSearch}"
                  </span>
                </CommandItem>
              )}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
