---
name: insert-latex-section-into-word
description: Convert a selected LaTeX section, including native equations, tables, figures, citations, and comments, into a self-contained section inside an existing .docx while preserving the destination document, matching its styles and pagination, and recording the insertion as genuine Word Track Changes under an explicit author. Use for manuscript/SI revisions, collaborator-ready Word redlines, moving a section from an older Word donor into a newer Word base, or repairing LaTeX-to-Word conversions that contain linked media, Unicode equations, broken references, or inconsistent formatting.
---

# Insert a LaTeX Section into Word

Produce a collaborator-ready DOCX that opens without any source files. Preserve the destination as the sole base, embed every added asset, use native Word math, and render the result before delivery.

Also invoke the available Word/DOCX document skill. Invoke the PDF skill when PDF figures or PDF visual comparison are involved. Obey their render-and-verify requirements.

## Non-negotiable outcome

- Never overwrite the TeX source, donor DOCX, destination DOCX, or supplied figures.
- Copy the destination DOCX first; patch only the required OOXML parts.
- Preserve every pre-existing destination media part byte-for-byte.
- Embed added figures under `word/media/`; never use linked images, `altChunk`, linked OLE objects, or dependencies on another DOCX/PDF.
- Store equations as native OMML (`m:oMath`/`m:oMathPara`), not plain Unicode math, equation screenshots, or linked objects.
- Record inserted content as genuine Word revisions and set the requested `w:author` explicitly.
- Preserve destination comments/revisions and migrate comments anchored inside the inserted block.
- Match styles by their human-readable style names, never by donor style IDs.
- Reconcile and renumber citations against the destination bibliography.
- Deliver a new DOCX only after structural and visual QA passes.

## Choose the workflow

1. If only LaTeX exists, build a small standalone donor DOCX first, then transplant its finalized section.
2. If a satisfactory older DOCX already contains the section, use that DOCX as the donor and skip reconversion.
3. If the destination already contains the section, compare content before editing; replace only when the user requested replacement. Never duplicate it.

Treat filenames and modification times only as hints. Record an explicit role manifest with absolute paths and SHA-256 values for TeX source, rendered proof, donor, destination, figures, bibliography, and output. If two donor/destination assignments remain plausible after structural inspection, ask before writing.

## Phase 1: Inspect and inventory

Snapshot every input before reading it, especially in a shared workspace:

```bash
python3 scripts/fingerprint_inputs.py --output /private/tmp/latex-word-inputs.json \
  source.tex donor.docx destination.docx figure.pdf
```

Run the inspector on both donor and destination:

```bash
python3 scripts/inspect_docx.py donor.docx --around "Section heading"
python3 scripts/inspect_docx.py destination.docx --around "Following heading"
```

Record:

- the exact LaTeX boundary, such as `\label{sec:...}`, and the next peer-or-higher heading;
- the destination paragraph before and after the insertion point;
- local heading, body, caption, bibliography, list, and table style names;
- destination reference maximum and any duplicate DOI/title already present;
- donor block relationships, comments, tracked-change authors, and native equation count;
- whether tables, figures, and discussion paragraphs use page breaks locally.
- active TeX cross-references that must resolve to destination bookmarks/captions, plus adjacent source comments that contain merge instructions or unresolved decisions.

Use direct `w:body` children for block boundaries. Do not locate a section only by global paragraph number because table-cell paragraphs distort such indices.

Read [references/latex-conversion.md](references/latex-conversion.md) before converting raw TeX. Read [references/ooxml-transplant.md](references/ooxml-transplant.md) before changing a DOCX package.

## Phase 2: Create a clean donor from LaTeX

Extract only the requested section plus the minimum preamble/macros it requires. Ignore unrelated TeX sections. Resolve every local dependency referenced by that section: figures, tables, labels, bibliography keys, custom commands, and units.

Use Pandoc for the first-pass DOCX because it converts TeX math to OMML. Convert PDF-only figures to lossless, high-resolution PNG before conversion. For example:

```bash
pdftoppm -png -r 300 -singlefile figure.pdf figure
pandoc section.tex --from=latex --to=docx --standalone --output=section-donor.docx
```

Repair unsupported formulas in Microsoft Word with `Alt+=`, select LaTeX input mode, enter the LaTeX source, and convert to Professional display. Never leave a formula as linear Unicode text. Use the source TeX as semantic authority and the rendered PDF as visual authority.

Finalize the donor before transplanting:

- apply the intended heading hierarchy;
- use destination-equivalent body/caption/table styles;
- embed each image inline;
- resolve the active `\includegraphics` path from the pinned TeX snapshot and require approval before splitting, combining, or replacing figures;
- put each table or figure on its own page when that matches the destination's spacious layout;
- put long discussion units on separate pages when nearby SI pages follow that convention;
- make the whole inserted section genuine tracked content under the requested author;
- remove reviewer-only comments unless the user asks to keep them.

## Phase 3: Transplant into the latest destination

Prefer the bundled transplanter after the donor section is visually correct and tracked:

```bash
python3 scripts/transplant_tracked_block.py \
  --donor section-donor.docx \
  --base latest-manuscript.docx \
  --output latest-manuscript-with-section.docx \
  --start-text "Inserted section heading" \
  --before-text "Existing following heading" \
  --author "Author Name" \
  --copy-reference 63 \
  --copy-reference 64
```

The script must operate on a copy of the destination, map style names, copy block relationships/media, remap relationship/revision/comment/paragraph/drawing IDs, append selected references after the bibliography field, update superscript citations, and enable Track Changes. It must update only changed ZIP entries so large existing media are not recompressed.

Use `--citation-map OLD=NEW` for a donor citation whose work already exists in the destination bibliography. Use `--no-comments` only when the user explicitly does not want donor comments.

If the donor block is not already tracked, use Microsoft Word to insert it with Track Changes enabled and the correct author; do not fake revision color or underlining.

## Phase 4: Reconcile references

Treat references as document-global state:

1. Identify only works cited by the inserted section.
2. Normalize DOI/title text and reuse any matching destination reference number.
3. Determine the current destination maximum from the visible bibliography, not from the donor.
4. Assign sequential numbers only to genuinely new works.
5. Update every superscript citation atomically, including comma lists and ranges.
6. Append new references after the existing Zotero bibliography field end so Zotero does not silently overwrite them.
7. Track the new bibliography text under the requested author.

Do not copy the donor's entire bibliography and do not assume its reference numbers remain valid.

Reject unresolved `[?]`, `??`, visible citation keys, and stale figure/table values. Reconcile numerical values across TeX, the supplied rendered proof, tables, and figure source data before insertion; ask for a content decision when they disagree.

Resolve cross-references semantically against destination headings, bookmarks, tables, and figures. Never carry over LaTeX-local strings such as “Section 5,” “Table 5.2,” or `2.x` without verifying what they mean in the destination.

## Phase 5: Verify

Run the verifier with the untouched destination as `--base`:

```bash
python3 scripts/verify_docx.py output.docx \
  --base latest-manuscript.docx \
  --expected-author "Author Name" \
  --tracked-block-start "Inserted section heading" \
  --tracked-block-before "Existing following heading" \
  --require-text "Inserted section heading" \
  --require-text "Figure S42A"
```

Pass `--min-added-math` using the donor-block equation inventory, and pass `--max-size-mib` when the journal or collaboration platform has an upload limit. Use `--allow-base-text-change` only for an explicitly approved edit outside the inserted block.

Immediately before packaging and again before delivery, confirm that no collaborator changed an input while work was in progress:

```bash
python3 scripts/fingerprint_inputs.py --check /private/tmp/latex-word-inputs.json
```

If the check fails, stop, re-inspect the new snapshot, and rebase the transplant. Never overwrite or revert the collaborator's newer file.

Then use the Word document skill's canonical renderer. If LibreOffice is unavailable or renders Word equations/revisions incorrectly, export a QA PDF with Microsoft Word itself. Inspect:

- the page before insertion;
- every page of the inserted section;
- the first page after insertion;
- every added figure and table page;
- the final bibliography page.

On temporary QA copies, render both All Markup and accepted/clean views. The rejected view must reproduce the pre-edit base except for explicitly allowed changes. Reopen/save a QA copy in the target Word version and recheck OMML after the round trip. Refresh fields/TOC only on a QA copy unless the user explicitly wants those updates in the deliverable.

Check at full size, not only contact-sheet scale. Read [references/qa.md](references/qa.md) for the gate.

## Delivery gate

Deliver only if all are true:

- the output opens in Word without a repair warning;
- all input fingerprints still match the inspected snapshot;
- deleting or moving every input still leaves the output fully functional;
- all destination media hashes match the untouched base;
- no newly introduced undefined styles, duplicate relationship IDs, broken internal targets, or external image links exist; report inherited base defects separately;
- comment anchors and comment records agree;
- inserted revisions identify the requested author;
- every inserted body child and content-bearing leaf is covered by genuine revision markup, and rejecting the changes reproduces the base;
- citation numbers and appended references agree;
- equations remain editable Word math and visually match the TeX/PDF;
- tables, figures, captions, headings, and page breaks match the surrounding document.
- the final artifact is outside temporary storage and satisfies any file-size/output-location constraint.

Report the output path, the donor/base roles used, the assigned reference range, the revision author, and the QA result. Explicitly state whether existing media were preserved byte-for-byte.
