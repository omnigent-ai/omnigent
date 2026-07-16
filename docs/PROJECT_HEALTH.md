# Project health

Use this policy when starting a substantial project, reading an unfamiliar area,
or planning a change that crosses module boundaries. Its purpose is to keep the
repository understandable without repeatedly loading large source files or
reconstructing architectural knowledge from code.

Small, local changes do not need new documentation. Apply the requirements below
when the information is durable, affects multiple contributors or agents, or
would otherwise have to be rediscovered.

## Project-local artifacts

Keep health artifacts beside the project they describe. A top-level subsystem
can use its own directory (for example, `web/`); a cross-cutting project can use
an existing file under `designs/` or `docs/`. Link all applicable artifacts from
the project's README or design/plan so there is one obvious starting point.

For a substantial project, the starting document must identify:

- **Architecture:** link an `ARCHITECTURE.md`, a design document, or an
  equivalent section that names boundaries, dependencies, data/control flow,
  and ownership. Include a Mermaid, ASCII, or maintained image diagram when the
  system has several interacting components or a sequence is difficult to
  explain linearly.
- **Glossary:** link `GLOSSARY.md` or an equivalent section defining
  project-specific terms, acronyms, and names that could be confused. Treat the
  glossary as required planning input when terminology is part of the design.
- **Invariants:** link `INVARIANTS.md` or an equivalent section listing
  properties that must remain true across components, state transitions, or
  failure paths. Each invariant should point to its enforcement or test where
  one exists.
- **Operational knowledge:** link relevant runbooks, generated schemas, API
  contracts, migration notes, or dependency graphs instead of duplicating them.

A single project README or design document may contain all of these sections;
separate files are useful only when the material is large or independently
maintained. Do not create empty placeholder documents.

## File size and context budget

Large files cost review time and model context. Before reading a file in full,
inspect its size and structure (`wc -l`, symbol search, headings, or a targeted
range), then load only the relevant sections. Search for callers, tests,
contracts, and project-health artifacts before expanding the read. Prefer
repository search and narrow slices over repeated full-file reads.

There is no universal line limit, but a source file approaching **500 lines** or
a document approaching **1,000 lines** requires an explicit choice in the plan:
read it selectively, split it along a real boundary, or record why keeping it
whole is clearer. Generated and vendored files are exempt from splitting, but
must be identified so contributors do not spend context analyzing them as
hand-maintained source.

When a changed file grows past these guideposts, reviewers should expect the PR
to explain the boundary decision. Avoid mechanical splitting that hides a
cohesive concept or creates circular dependencies.

## Sustainable workflow

### Before planning or coding

1. Read the nearest `AGENTS.md`, README, design/plan, glossary, and invariants.
2. Map the affected modules, entry points, callers, tests, contracts, and
   generated files with search before opening large files in full.
3. Record the architecture boundary and invariants the change must preserve.
4. Decide whether a diagram, glossary entry, invariant, or large-file boundary
   must be created or updated as part of the change.
5. Put those documentation updates and their verification in the implementation
   plan, not in a follow-up backlog.

### While changing the project

- Keep terminology consistent with the glossary and update it when introducing
  a durable new term.
- Turn new cross-cutting assumptions into explicit invariants and, where
  practical, tests or validation.
- Update diagrams and dependency/data-flow descriptions in the same change that
  makes them inaccurate.
- Re-check file growth and dependency direction before adding another concern
  to an already large module.

### Before review

- Confirm links and diagrams render and referenced files exist.
- Verify documented commands, tests, schemas, and generated artifacts as
  applicable.
- In the PR, state which health artifacts changed, or why none were needed.
- Call out deliberate exceptions to the size guideposts and the reasoning.

## Planning checklist

Copy this into a design or implementation plan when the project is substantial:

```markdown
## Project health

- [ ] Starting document and ownership identified
- [ ] Architecture boundaries and diagram/graph needs assessed
- [ ] Glossary read and updated if terminology changes
- [ ] Invariants read, preserved, and linked to enforcement/tests where possible
- [ ] Large files identified; selective-read or split decisions recorded
- [ ] Contracts, schemas, runbooks, and generated artifacts identified
- [ ] Health artifacts included in implementation and verification scope
```
