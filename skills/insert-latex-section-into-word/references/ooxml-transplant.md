# OOXML transplant rules

## Contents

1. Package-preserving update
2. Structural boundaries
3. Identifier remapping
4. Relationships and media
5. Styles
6. Comments
7. Tracked changes
8. Bibliography endpoint

## 1. Package-preserving update

A DOCX is a ZIP package. To preserve a large destination exactly:

1. Copy the destination DOCX to a new output path.
2. Parse only the parts that require changes.
3. Write changed parts to a temporary directory.
4. Deterministically delete only the changed entries from a staging copy, add their replacements, verify their bytes, and atomically rename the completed package. Do not rely on ZIP timestamps.
5. Never rebuild the full archive with `python-docx` or a new `zipfile` when byte-preserving existing media matters.

Expected changed parts usually include:

- `word/document.xml`
- `word/_rels/document.xml.rels`
- `word/settings.xml`
- comment/people parts when migrated comments exist
- `[Content_Types].xml` only when a new part or unsupported extension requires it
- newly added `word/media/*`

## 2. Structural boundaries

Use direct children of `w:body` (`w:p`, `w:tbl`, `w:sdt`, and `w:sectPr`) to select a contiguous block. Use exact visible heading text plus the next existing peer heading as the end/insertion marker.

Reject ambiguous markers. Never cut only paragraphs while leaving the section's tables or drawing paragraphs behind.

## 3. Identifier remapping

Remap imported identifiers to values unused by the destination:

- revision `w:id` on elements carrying `w:author`;
- comment IDs on `commentRangeStart`, `commentRangeEnd`, `commentReference`, and `comments.xml`;
- `w14:paraId` for imported paragraphs;
- `wp:docPr/@id`;
- drawing `wp14:anchorId` and `wp14:editId`;
- relationship IDs.

Also check bookmarks and drawing IDs if the selected block contains them. Keep comment durable IDs and extension mappings synchronized.

## 4. Relationships and media

Scan every imported `@r:id`, `@r:embed`, and `@r:link`. Resolve it through the donor part's relationship file. Copy each required internal image to a unique destination media name and create a fresh relationship.

Copy external hyperlinks only when they are intentional. Fail on unsupported relationship types instead of producing a partially functional document.

Use the existing content type default for PNG/JPEG when present; otherwise add the correct default/override.

## 5. Styles

Read `word/styles.xml` in both files. Resolve donor `w:pStyle`, `w:rStyle`, and `w:tblStyle` IDs to donor style names, then resolve the same names to destination IDs. Fail when an essential style name is missing; do not guess by ID.

Direct run/paragraph/table formatting can be copied after style remapping.

## 6. Comments

For comments anchored inside the imported block:

- copy the matching `w:comment` entry;
- remap its numeric comment ID and all three anchors;
- copy matching entries in `commentsExtended.xml`, `commentsIds.xml`, and `commentsExtensible.xml` when present;
- add the author to `people.xml` when absent;
- preserve all destination comments;
- validate a one-to-one start/end/reference record for each comment.

If the destination lacks comment parts, create them from clean roots and add their document relationships/content-type overrides.

## 7. Tracked changes

Use real revision markup:

- inserted runs: `w:ins` with `w:id`, `w:author`, and `w:date`;
- inserted paragraph marks: `w:pPr/w:rPr/w:ins`;
- deleted runs: `w:del` and `w:delText`;
- table rows/cells: preserve valid row/cell revision markup from a Word-generated donor.

Set `<w:trackRevisions/>` in `word/settings.xml` so subsequent collaborator edits remain tracked. Remap imported revision IDs and set the requested author explicitly. Do not use red font or underline as a visual imitation.

## 8. Bibliography endpoint

Zotero bibliographies commonly end in an otherwise blank paragraph containing `w:fldChar w:fldCharType="end"`. Append the first manual tracked reference after that field character, then add further tracked Bibliography paragraphs before `w:sectPr`.

If no field end exists, append after the last numbered Bibliography paragraph. Preserve the destination numbering style and indentation.
