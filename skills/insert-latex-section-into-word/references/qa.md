# DOCX acceptance and visual QA

## Structural gate

Require all of the following:

- ZIP integrity passes.
- Every original destination part still exists.
- Every original `word/media/*` hash matches the untouched destination.
- Only intended pre-existing OOXML parts changed.
- Every newly used style ID exists in `styles.xml`; report inherited base defects separately.
- Relationship IDs are unique and every internal target exists after normalized relative-path resolution.
- Added media are reachable and no new orphan media or duplicate drawing-property IDs exist.
- No image relationship has `TargetMode="External"`; no drawing uses `r:link`.
- Revision IDs introduced by the transplant are unique and carry the requested author.
- `<w:trackRevisions/>` is enabled.
- Comment record IDs equal the start/end/reference anchor IDs.
- Expected headings, captions, and references occur exactly once.
- Every direct body child and content-bearing leaf inside the declared inserted range is covered by genuine revision markup.
- Native equation count is at least destination count plus donor-block count.
- Citation numbers in the section match the appended/reused references.
- Input hashes and modification metadata still match the preflight snapshot.
- No unresolved `[?]`, `??`, visible TeX command, or citation key remains.
- Every original destination body element remains in order outside the declared change allowlist.

## Visual gate

Run the canonical DOCX renderer from the available Word document skill. If `soffice` is missing or fidelity is questionable, export with Microsoft Word:

```applescript
with timeout of 600 seconds
  tell application "Microsoft Word"
    set qaDoc to document "QA-copy.docx"
    save as qaDoc file name (POSIX file "/private/tmp/QA-copy.pdf") file format format PDF
    close qaDoc saving no
  end tell
end timeout
```

Search the PDF text to find page numbers for the inserted start heading, each figure/table caption, the following original heading, and the added references. Render that complete range plus the bibliography page to PNG.

Create QA copies for both All Markup and accepted/clean views. Reject all inserted revisions in another temporary copy and compare it with the untouched base; differences outside the declared allowlist indicate incomplete or malformed tracked coverage. Reopen/save a QA copy in the target Word version, then confirm the equation count and visually sensitive constructs still survive.

Inspect every affected page for:

- missing or clipped text;
- incorrect heading level/font/size;
- equations rendered linearly, as boxes, or with missing accents/scripts;
- empty or deleted OMML objects that render as unexplained short rules, strokes, or spacing artifacts;
- table overflow, row splits, or inconsistent borders;
- narrow columns that split short header words awkwardly instead of using a deliberate width or line break;
- stretched, pixelated, duplicated, or missing images;
- captions separated from figures;
- unintended blank pages or dense pages that violate local spacing;
- explicit page breaks that strand only a few lines of a paragraph on an otherwise empty page;
- revision balloons obscuring content;
- reference numbering gaps or duplicates.

Open the final DOCX itself in Word once. A successful PDF export is not enough if Word reports package repair.

Inspect cached metadata and field results separately. Refresh fields and the TOC on a temporary copy, compare pagination/navigation, and only apply those updates to the deliverable when requested.
