---
name: api-docs
description: Document a module or public API surface (functions, classes, CLI commands, endpoints) from the code itself. Use when the user asks for API reference, to document a module, or to write usage docs for a public interface.
---

# api-docs — document a public API surface from the code

Produce reference docs that match the code exactly, derived from the source, not
from assumptions about what the API "probably" does.

## Read the surface

Identify the public surface to document (a module, class, CLI command group, or
set of endpoints). Then have the researcher read it carefully:
- Dispatch the researcher (`purpose: explore`) to enumerate the public
  entry points and report each one's real signature, parameters, defaults,
  return shape, and raised errors — with file:line evidence.
- Prefer what the code declares (signatures, type hints, docstrings, default
  values) over prose descriptions. Public vs. private follows the project's
  convention (e.g. a leading underscore, or an `__all__` / export list).

## Structure

For each entry point:

    ### `<name>(<signature>)`

    <one-line summary of what it does>

    **Parameters**
    - `<name>` (`<type>`, default `<value>`) — <meaning>

    **Returns** — `<type>`: <meaning>

    **Raises** — `<Error>`: <when>

    **Example**
    ```
    <minimal, runnable usage>
    ```

## Write the entries

- Keep the summary to one line; put detail in the parameter and example
  sections.
- Document every public parameter, including defaults, in the order they appear
  in the signature.
- Give one minimal example per entry point that actually runs against the
  documented signature.
- Do not document private/internal helpers unless the user asks; a reference is
  the contract, not a code tour.

## Verify

Signatures, defaults, and error types drift fastest, so route the finished
reference through the `reviewer` (`purpose: review`) to confirm every signature
and default matches the current code.
