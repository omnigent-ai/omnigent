# Markdown editor performance results

The October 18-20, 2026 optimization work is complete through
[PR #10917](https://github.com/omnigent-ai/omnigent/pull/10917), merged on
October 20. Opening, editing, and oversized-file handling all improved.
The [Markdown editor feature map](../feature-map/markdown-editor.md) defines
the maintained verification journeys and recording protocol.

## Overall outcome

- One file viewer survives desktop/phone resizing, replacing two mounted
  viewers and preserving unsaved edits, selection, and comment drafts.
- Synchronous editing work on the standard 256 KiB document fell from
  46.10 ms to 2.30 ms per character, about 20x less work. Full-document
  serialization moved from every keystroke to autosave.
- The reported single-line stack overflow was reproduced and fixed in heading
  recognition. The server's truncated prefix can be displayed read-only,
  with the complete file available to download.
- Markdown above 1 MiB now opens as editable source. On the synthetic 2 MiB
  mixed document, readiness fell from 13.759 s in rich view to 1.150 s in
  source, about 12x faster. The longest task fell from 12.008 s to 0.186 s.

There is no single controlled start-to-finish speedup for this project.
Historical results for the standard 256 KiB rich-editor workload went from
20.46 s before the viewer/layout fixes to 3.407 s after deferring language
options, roughly a sixfold reduction using the original readiness probe.
That is a trend across separately recorded milestones, not a fresh paired
benchmark. The corrected probe later measured about 2.4 s; its lower number
must not all be credited to application optimization.

## Measured changes by stage

Each row compares the baseline and candidate for its linked PR. These gains
are not additive or multiplicative across rows. In particular, the viewer
and selection changes were tested independently against the same baseline.
The source PRs contain exact revisions, sample ranges, and recordings.

| Change | Workload and metric | Before | After | Evidence |
| --- | --- | ---: | ---: | --- |
| TipTap 3.30.5 to 3.31.4 | 256 KiB ordered-list parse, Node median | 7.45 s | 0.07 s | [#10698](https://github.com/omnigent-ai/omnigent/pull/10698) |
| TipTap upgrade | 256 KiB mixed parse, Node median | 11.2 s | 6.0 s | [#10698](https://github.com/omnigent-ai/omnigent/pull/10698) |
| One persistent viewer | Standard 256 KiB readiness, original probe | 20.46 s | 11.69 s | [#10715](https://github.com/omnigent-ai/omnigent/pull/10715) |
| Selection capture only for the containing node | Standard 256 KiB readiness, original probe | 20.46 s | 9.75 s | [#10723](https://github.com/omnigent-ai/omnigent/pull/10723) |
| Selection capture only for the containing node | Standard 256 KiB native selection reads | 1,144 | 6 | [#10723](https://github.com/omnigent-ai/omnigent/pull/10723) |
| Bound task-list and table scans | Standard 256 KiB readiness, original probe | 5.847 s | 4.199 s | [#10751](https://github.com/omnigent-ai/omnigent/pull/10751) |
| Serialize at autosave | Standard 256 KiB synchronous update per character | 46.10 ms | 2.30 ms | [#10772](https://github.com/omnigent-ai/omnigent/pull/10772) |
| Serialize at autosave | Serializations during a 21-character burst | 21 | 0 | [#10772](https://github.com/omnigent-ai/omnigent/pull/10772) |
| Defer code-block language options | Standard 256 KiB readiness, original probe | 4.224 s | 3.407 s | [#10783](https://github.com/omnigent-ai/omnigent/pull/10783) |
| Avoid per-node renderer-registry copies | Mixed 1 MiB readiness, corrected probe | 7.794 s | 7.009 s | [#10838](https://github.com/omnigent-ai/omnigent/pull/10838) |
| Automatic source fallback above 1 MiB | Mixed 2 MiB readiness, rich versus source | 13.759 s | 1.150 s | [#10917](https://github.com/omnigent-ai/omnigent/pull/10917) |
| Automatic source fallback above 1 MiB | Mixed 2 MiB longest main-thread task | 12.008 s | 0.186 s | [#10917](https://github.com/omnigent-ai/omnigent/pull/10917) |

The final registry comparison measured rich-view readiness of 1.201 s at
64 KiB and 2.408 s at 256 KiB. The 256 KiB registry improvement was small,
with overlapping samples; its useful gain was about 10% at 1 MiB.

## Correctness and repeatability

[PR #10762](https://github.com/omnigent-ai/omnigent/pull/10762) replaced the
stack-exhausting underline-heading regex path with an iterative scanner.
The original reproduction was an 11 MiB single-line file whose loaded
10 MiB prefix crashed in `RegExp.exec`. The follow-up
[#10769](https://github.com/omnigent-ai/omnigent/pull/10769) strengthened
compatibility checks. This was a separate failure from slow parsing and DOM
construction; faster tokenizers alone did not fix it.

[PR #10729](https://github.com/omnigent-ai/omnigent/pull/10729) established
the fixed review journey and 64/256 KiB fixtures. Subsequent validation covered
code-block creation, language changes, save/reload, Find, view switching,
resizing, and comment anchoring. Distinct scenarios cover unsaved drafts and
the mobile close guard.

For the source-fallback implementation, nine isolated real-server journeys
passed, including complete-file persistence, shrinking and continuing to
edit, comments, links, and complete downloads of truncated files. Its final
review cleanup passed 360 component tests, with one existing expected failure,
and all four selected browser journeys. These are the recorded checks on the
revisions named in #10917, not fresh tests of every later main revision.

## Measurement boundaries

- Browser performance comparisons used production builds, synthetic fixtures,
  mocked backend APIs, Chromium 148.0.7778.96, and a 1600 x 1000 viewport.
  The standard comparisons report three-run medians. They are local evidence,
  not latency guarantees or representative measurements for every device.
- [PR #10828](https://github.com/omnigent-ai/omnigent/pull/10828) removed a
  Playwright accessibility-tree snapshot from the readiness probe. A diagnostic
  profile attributed about 0.89 s to that assertion at 256 KiB and 3.70 s at
  1 MiB. That PR changed measurement only. Do not compare old-probe numbers
  directly with `readiness_probe: terminal-marker` results as a product gain.
- The first upgrade used a different browser fixture and single-run timings
  of 78.4 s before and 60.6 s after at approximately 256 KiB. Those values
  cannot be combined with the later standard-fixture results.
- The 2 MiB fallback comparison waits for the rich document's terminal marker
  before, and Monaco's first visible source lines after, then two animation
  frames in both cases. It measures the usable entry surface, not a faster
  rich parser. The actual fixture contains 2,097,164 UTF-8 bytes.
- CPU profiles identify costs but their sampled times are not additive to
  wall-clock timings. Slow-motion review videos show correctness, not speed.

## Final policy and remaining work

The threshold is strictly above 1,048,576 UTF-8 bytes. Exactly 1 MiB retains
the normal rich-view choice. Fully loaded source files remain editable;
truncated files remain read-only. The choice stays fixed for a file visit so
an autosave crossing the threshold cannot replace an editor containing newer
unsaved changes. Navigate to another file and back, or reload, to reassess it.

Size is a conservative proxy for rendering cost. Dense files below the limit
can still stall, and a small rich document can grow past the limit during the
same visit. Before the fallback, candidate-only 2 MiB probes took about
13.6 s for mixed content, 12.6 s for tables, and 1.7 s for prose. This is why
the policy uses 1 MiB rather than allowing rich rendering up to 2 MiB.

Further performance work can wait for a concrete user case or regression.
Keep these maintenance and correctness follow-ups separate:

1. Reassess the local TipTap selection and registry patches when upgrading.
   Their removal conditions and validation are in
   [patches/README.md](../patches/README.md). The bounded tokenizer wrapper
   also has an upstream-removal TODO.
2. The Changes journey observed a saved edit in the filesystem and diff API
   while the displayed diff stayed stale until reload. That journey is not
   counted as fully passing; it remains a separate cache-refresh follow-up.
3. The panel-collapse scroll assertion failed on both unchanged main and the
   source-fallback candidate, although a focused run passed. Treat it as a
   separate navigation/test-stability follow-up.
4. Safari and native mobile behavior were not validated by these Chromium
   runs. Browser viewport resizing is not native-device verification.

The replacement implementations supersede the closed proposals
[#10376](https://github.com/omnigent-ai/omnigent/pull/10376) and
[#9194](https://github.com/omnigent-ai/omnigent/pull/9194).
[PR #10917](https://github.com/omnigent-ai/omnigent/pull/10917) also supersedes
[#6528](https://github.com/omnigent-ai/omnigent/pull/6528); its proposal is no
longer needed now that editable source fallback has landed.
