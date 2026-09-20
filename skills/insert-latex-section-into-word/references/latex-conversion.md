# LaTeX-to-Word conversion notes

## Contents

1. Section extraction
2. Native equations
3. Figures
4. Tables and headings
5. Citations

## 1. Section extraction

Locate the requested `\label{...}` and its owning section command. Include content until the next command at the same or higher hierarchy. Account for commented section commands and commands inside verbatim-like environments.

Inventory dependencies with `rg` before conversion:

```bash
rg -n '\\(section|subsection|subsubsection|label|ref|cite|includegraphics|input|include|newcommand|DeclareMathOperator)' source.tex
```

Create an isolated temporary TeX file containing:

- document class and packages needed by the selected section;
- custom commands used by the selected section;
- only the selected section body;
- only cited bibliography entries when a `.bib` workflow is required.

Do not silently expand into unrelated sections.

Treat revision-color macros such as `\rev{...}` or `\purev{...}` as content wrappers, not as Word revisions. Preserve their enclosed content, remove the macro syntax, and create genuine Word Track Changes separately. Decide explicitly whether reviewer boxes, commented-out experiments, and edits targeting pages outside the selected section belong in scope.

## 2. Native equations

Prefer Pandoc-generated OMML for bulk conversion. Confirm the DOCX contains `m:oMath` or `m:oMathPara` elements.

For formulas Pandoc cannot preserve:

1. Open the destination/donor in Microsoft Word.
2. Press `Alt+=` to create an equation.
3. Set the equation input syntax to LaTeX rather than UnicodeMath.
4. Paste the exact TeX expression without display delimiters.
5. Convert to Professional layout.
6. Compare symbols, accents, scripts, delimiters, matrices, operators, and equation numbers with the source PDF.

Never satisfy “LaTeX equation” by inserting visible TeX source as body text. Word stores editable equations as OMML even when entered through LaTeX mode.

Preserve equation numbering as ordinary right-aligned Word content or a borderless two-column/three-column layout consistent with the destination. Avoid rasterized equations.

## 3. Figures

Use an existing PNG when supplied. For a PDF-only figure, render a lossless PNG at 300 dpi or higher:

```bash
pdftoppm -png -r 300 -singlefile input.pdf output-stem
```

Verify dimensions and content before insertion. Insert as `wp:inline`, sized to the usable text width while preserving aspect ratio. Use one figure and its caption per page when that is the local convention.

Resolve the active graphic path from the pinned TeX source, not from the label or basename alone. Detect duplicate basenames. Treat splitting a multi-panel figure, combining panels, or substituting a newer plot as a content transformation requiring an explicit user instruction.

An embedded figure requires all three:

- a drawing in `word/document.xml` with `r:embed`;
- an image relationship in `word/_rels/document.xml.rels`;
- the actual binary under `word/media/` with a supported content type.

Reject `r:link`, external image relationships, PDF OLE links, and references to the source filesystem.

## 4. Tables and headings

Infer hierarchy from both TeX commands and nearby Word styles. Map by style name:

| LaTeX role | Word role |
|---|---|
| section-level peer | same named heading style as neighboring peer sections |
| lower-level heading | next local heading level |
| prose | local body/No Spacing style |
| figure/table caption | local Caption style |
| bibliography entry | local Bibliography style |

Do not copy raw style IDs such as `af6` or `21`; Word can assign different IDs to styles with identical names in different files.

Prefer native Word tables. Keep rows together only when it prevents an awkward split. If nearby SI design uses one table per page, set `pageBreakBefore` on the caption or preceding paragraph instead of inserting many empty lines.

Assert row/column counts and important cell semantics. Recompute derived differences, percentages, totals, and bolded best values from the stated inputs; block delivery when TeX, plot data, and table arithmetic disagree.

## 5. Citations

Resolve citation keys to a temporary numbered donor bibliography, then reconcile against the destination. Match existing works by DOI first, normalized title second, and author/year only as a fallback.

Keep in-text numeric citations as superscript runs. When renumbering, replace numeric tokens within a citation run while preserving punctuation. For example, donor `64,65` can become destination `67,68` without splitting its formatting.

Do not place manually added references inside a live Zotero field result. Append after its `w:fldCharType="end"` marker and retain the destination Bibliography style.

Before conversion, reject unresolved citation placeholders (`[?]`, `??`, or literal citation keys). Compare figure labels and values with the current TeX and plotting source rather than trusting an older proof PDF or a similarly named stale figure.
