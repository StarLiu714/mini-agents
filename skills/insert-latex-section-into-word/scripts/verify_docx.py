#!/usr/bin/env python3
"""Verify that a transplanted DOCX is self-contained and preserves its base."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
from collections import Counter
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from lxml import etree


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"w": W, "m": M, "r": R, "wp": WP, "ct": CT}


def parse(data: bytes) -> etree._Element:
    return etree.fromstring(data)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def media_names(package: ZipFile) -> list[str]:
    return sorted(
        name for name in package.namelist()
        if name.startswith("word/media/") and not name.endswith("/")
    )


def normalize_target(source_part: str, target: str) -> str:
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(source_part), target)
    ).lstrip("/")


def relationship_source(rel_part: str) -> str:
    if rel_part == "_rels/.rels":
        return ""
    rel_dir = posixpath.dirname(rel_part)
    if posixpath.basename(rel_dir) != "_rels" or not rel_part.endswith(".rels"):
        raise ValueError(f"Invalid relationship part path: {rel_part}")
    source_dir = posixpath.dirname(rel_dir)
    source_name = posixpath.basename(rel_part)[:-5]
    return posixpath.join(source_dir, source_name)


def referenced_media(package: ZipFile) -> set[str]:
    result: set[str] = set()
    for rel_part in package.namelist():
        if not rel_part.endswith(".rels"):
            continue
        root = parse(package.read(rel_part))
        source = relationship_source(rel_part)
        for rel in root:
            if rel.get("TargetMode") == "External":
                continue
            target = normalize_target(source, rel.get("Target", ""))
            if target.startswith("word/media/"):
                result.add(target)
    return result


def canonical(element: etree._Element) -> bytes:
    return etree.tostring(element, method="c14n")


def element_text(element: etree._Element) -> str:
    return "".join(element.itertext()).strip()


def exact_body_index(body: etree._Element, wanted: str, start: int = 0) -> int:
    hits = [
        index for index, child in enumerate(body)
        if index >= start and element_text(child) == wanted
    ]
    if len(hits) != 1:
        raise ValueError(f"Expected one direct-body match for {wanted!r}, found {hits}")
    return hits[0]


def tracked_block_violations(roots: list[etree._Element]) -> list[str]:
    violations: list[str] = []
    leaf_xpath = (
        ".//w:t[normalize-space(.) != ''] | "
        ".//m:t[normalize-space(.) != ''] | "
        ".//w:delText[normalize-space(.) != ''] | "
        ".//w:instrText[normalize-space(.) != ''] | "
        ".//w:drawing | .//w:pict | .//w:object | "
        ".//w:br | .//w:tab | .//w:sym | .//w:fldChar"
    )
    for index, root in enumerate(roots):
        root_tag = etree.QName(root).localname
        root_is_inserted = root.tag in {
            f"{{{W}}}ins",
            f"{{{W}}}moveTo",
        } and bool(root.get(f"{{{W}}}author") and root.get(f"{{{W}}}id"))
        if not root.xpath(
            ".//*[@w:author and @w:id] | self::*[@w:author and @w:id]",
            namespaces=NS,
        ):
            violations.append(f"body child {index} has no revision markup")
            continue
        if not root_is_inserted and root.tag == f"{{{W}}}p":
            if not root.xpath(
                "./w:pPr/w:rPr/w:ins[@w:author and @w:id] | "
                "./w:pPr/w:rPr/w:moveTo[@w:author and @w:id]",
                namespaces=NS,
            ):
                violations.append(
                    f"body child {index} paragraph mark is not tracked as inserted"
                )
        elif not root_is_inserted and root.tag == f"{{{W}}}tbl":
            rows = root.xpath(".//w:tr", namespaces=NS)
            if not rows:
                violations.append(f"body child {index} is an empty untracked table")
            for row_index, row in enumerate(rows):
                if not row.xpath(
                    "./w:trPr/w:ins[@w:author and @w:id] | "
                    "ancestor::w:ins[@w:author and @w:id] | "
                    "ancestor::w:moveTo[@w:author and @w:id]",
                    namespaces=NS,
                ):
                    violations.append(
                        f"body child {index} table row {row_index} is not tracked as inserted"
                    )
                    if len(violations) >= 20:
                        return violations
        elif not root_is_inserted and root.tag not in {
            f"{{{W}}}p",
            f"{{{W}}}tbl",
        }:
            violations.append(
                f"body child {index} ({root_tag}) is not inside a block insertion"
            )
        for leaf in root.xpath(leaf_xpath, namespaces=NS):
            if leaf.xpath(
                "ancestor::w:ins | ancestor::w:moveTo",
                namespaces=NS,
            ):
                continue
            if leaf.xpath(
                "ancestor::w:tr[w:trPr/w:ins] | "
                "ancestor::w:tc[w:tcPr/w:cellIns]",
                namespaces=NS,
            ):
                continue
            preview = element_text(leaf)[:60] or etree.QName(leaf).localname
            violations.append(
                f"body child {index} contains untracked {etree.QName(leaf).localname}: "
                f"{preview!r}"
            )
            if len(violations) >= 20:
                return violations
    return violations


def verify(
    path: Path,
    base_path: Path | None,
    expected_author: str | None,
    required: list[str],
    allowed_base_text_changes: list[str],
    minimum_added_math: int | None,
    max_size_mib: float | None,
    tracked_block_start: str | None,
    tracked_block_before: str | None,
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    report: dict = {"path": str(path.resolve()), "errors": errors, "warnings": warnings}
    report["size_mib"] = path.stat().st_size / 2**20 if path.exists() else None
    if max_size_mib is not None and path.exists() and report["size_mib"] > max_size_mib:
        errors.append(
            f"File size {report['size_mib']:.2f} MiB exceeds limit {max_size_mib:.2f} MiB"
        )

    try:
        package = ZipFile(path)
    except (BadZipFile, FileNotFoundError) as exc:
        return {**report, "errors": [f"Cannot open DOCX ZIP: {exc}"]}

    with package:
        duplicate_members = sorted(
            name for name, count in Counter(package.namelist()).items() if count > 1
        )
        report["duplicate_zip_members"] = duplicate_members
        if duplicate_members:
            errors.append(f"Duplicate ZIP members: {duplicate_members}")
        bad = package.testzip()
        if bad:
            errors.append(f"ZIP CRC failure: {bad}")
        names = set(package.namelist())
        for required_part in (
            "[Content_Types].xml",
            "word/document.xml",
            "word/styles.xml",
            "word/settings.xml",
            "word/_rels/document.xml.rels",
        ):
            if required_part not in names:
                errors.append(f"Missing required part: {required_part}")
        if errors:
            return report

        doc = parse(package.read("word/document.xml"))
        styles = parse(package.read("word/styles.xml"))
        settings = parse(package.read("word/settings.xml"))
        rels = parse(package.read("word/_rels/document.xml.rels"))
        content_types = parse(package.read("[Content_Types].xml"))

        body_text = "".join(doc.itertext())
        required_counts = {marker: body_text.count(marker) for marker in required}
        report["required_text_counts"] = required_counts
        for marker, count in required_counts.items():
            if count != 1:
                errors.append(
                    f"Required text must occur exactly once: {marker!r} (found {count})"
                )

        body = doc.find(f"{{{W}}}body")
        if (tracked_block_start is None) != (tracked_block_before is None):
            errors.append(
                "--tracked-block-start and --tracked-block-before must be supplied together"
            )
        elif tracked_block_start is not None and body is not None:
            try:
                block_start = exact_body_index(body, tracked_block_start)
                block_end = exact_body_index(body, tracked_block_before, block_start + 1)
            except ValueError as exc:
                errors.append(str(exc))
            else:
                tracking_violations = tracked_block_violations(
                    list(body)[block_start:block_end]
                )
                report["tracked_block_children"] = block_end - block_start
                report["tracked_block_violations"] = tracking_violations
                if tracking_violations:
                    errors.append(
                        "Inserted block is not wholly covered by revision markup: "
                        + "; ".join(tracking_violations[:5])
                    )

        defined_styles = set(styles.xpath("//w:style/@w:styleId", namespaces=NS))
        used_styles = set(
            doc.xpath("//w:pStyle/@w:val | //w:rStyle/@w:val | //w:tblStyle/@w:val", namespaces=NS)
        )
        if "word/comments.xml" in names:
            comments_root = parse(package.read("word/comments.xml"))
            used_styles.update(
                comments_root.xpath(
                    "//w:pStyle/@w:val | //w:rStyle/@w:val | //w:tblStyle/@w:val",
                    namespaces=NS,
                )
            )
        else:
            comments_root = None
        undefined_styles = set(used_styles - defined_styles)
        inherited_undefined_styles: set[str] = set()
        if base_path is not None:
            with ZipFile(base_path) as style_base:
                base_styles_root = parse(style_base.read("word/styles.xml"))
                base_defined_styles = set(
                    base_styles_root.xpath("//w:style/@w:styleId", namespaces=NS)
                )
                base_doc_for_styles = parse(style_base.read("word/document.xml"))
                base_used_styles = set(
                    base_doc_for_styles.xpath(
                        "//w:pStyle/@w:val | //w:rStyle/@w:val | //w:tblStyle/@w:val",
                        namespaces=NS,
                    )
                )
                if "word/comments.xml" in style_base.namelist():
                    base_comments_for_styles = parse(style_base.read("word/comments.xml"))
                    base_used_styles.update(
                        base_comments_for_styles.xpath(
                            "//w:pStyle/@w:val | //w:rStyle/@w:val | //w:tblStyle/@w:val",
                            namespaces=NS,
                        )
                    )
                inherited_undefined_styles = base_used_styles - base_defined_styles
        new_undefined_styles = sorted(undefined_styles - inherited_undefined_styles)
        report["undefined_styles"] = sorted(undefined_styles)
        report["inherited_undefined_styles"] = sorted(inherited_undefined_styles)
        report["new_undefined_styles"] = new_undefined_styles
        if inherited_undefined_styles:
            warnings.append(
                f"Base already uses undefined styles: {sorted(inherited_undefined_styles)}"
            )
        if new_undefined_styles:
            errors.append(f"New undefined styles: {new_undefined_styles}")

        rel_ids = [rel.get("Id") for rel in rels]
        duplicate_rel_ids = sorted(key for key, value in Counter(rel_ids).items() if value > 1)
        report["relationship_count"] = len(rel_ids)
        report["duplicate_relationship_ids"] = duplicate_rel_ids
        if duplicate_rel_ids:
            errors.append(f"Duplicate relationship IDs: {duplicate_rel_ids}")

        broken_targets = []
        external_images = []
        altchunk_rels = []
        for rel in rels:
            rel_type = rel.get("Type", "")
            if rel_type.endswith("/aFChunk"):
                altchunk_rels.append(rel.get("Id"))
            if rel.get("TargetMode") == "External":
                if rel_type.endswith("/image") or rel_type.endswith("/oleObject"):
                    external_images.append((rel.get("Id"), rel.get("Target")))
                continue
            target = normalize_target("word/document.xml", rel.get("Target", ""))
            if target not in names:
                broken_targets.append((rel.get("Id"), target))
        report["broken_relationship_targets"] = broken_targets
        report["external_linked_images_or_ole"] = external_images
        if broken_targets:
            errors.append(f"Broken internal relationship targets: {broken_targets}")
        if external_images:
            errors.append(f"External linked images/OLE objects: {external_images}")
        if altchunk_rels or doc.xpath("//w:altChunk", namespaces=NS):
            errors.append("Document contains altChunk content")
        if doc.xpath("//@r:link", namespaces=NS):
            errors.append("Document contains drawing r:link attributes")

        used_rel_ids = set(doc.xpath("//@r:id | //@r:embed | //@r:link", namespaces=NS))
        undefined_rel_ids = sorted(used_rel_ids - set(rel_ids))
        report["undefined_used_relationship_ids"] = undefined_rel_ids
        if undefined_rel_ids:
            errors.append(f"Document uses undefined relationships: {undefined_rel_ids}")

        defaults = {
            el.get("Extension", "").casefold()
            for el in content_types.xpath("//ct:Default", namespaces=NS)
        }
        overrides = {
            el.get("PartName", "").lstrip("/")
            for el in content_types.xpath("//ct:Override", namespaces=NS)
        }
        missing_content_types = []
        for media in media_names(package):
            extension = Path(media).suffix.lstrip(".").casefold()
            if extension not in defaults and media not in overrides:
                missing_content_types.append(media)
        report["media_missing_content_type"] = missing_content_types
        if missing_content_types:
            errors.append(f"Media lacks content type declarations: {missing_content_types}")

        revisions = doc.xpath("//*[@w:author and @w:id]", namespaces=NS)
        revision_ids = [el.get(f"{{{W}}}id") for el in revisions]
        duplicate_revision_ids = sorted(
            key for key, value in Counter(revision_ids).items() if value > 1
        )
        revision_authors = Counter(el.get(f"{{{W}}}author") for el in revisions)
        report["revision_count"] = len(revisions)
        report["revision_authors"] = dict(revision_authors)
        report["duplicate_revision_ids"] = duplicate_revision_ids
        if duplicate_revision_ids:
            errors.append(f"Duplicate revision IDs: {duplicate_revision_ids[:20]}")
        if not settings.xpath("./w:trackRevisions", namespaces=NS):
            errors.append("Track Changes is not enabled in word/settings.xml")

        docpr_ids = doc.xpath("//wp:docPr/@id", namespaces=NS)
        duplicate_docpr_ids = {
            key for key, value in Counter(docpr_ids).items() if value > 1
        }
        report["duplicate_docpr_ids"] = sorted(duplicate_docpr_ids)

        start_ids = set(doc.xpath("//w:commentRangeStart/@w:id", namespaces=NS))
        end_ids = set(doc.xpath("//w:commentRangeEnd/@w:id", namespaces=NS))
        ref_ids = set(doc.xpath("//w:commentReference/@w:id", namespaces=NS))
        record_ids = (
            set(comments_root.xpath("//w:comment/@w:id", namespaces=NS))
            if comments_root is not None else set()
        )
        report["comment_ids"] = {
            "records": sorted(record_ids),
            "starts": sorted(start_ids),
            "ends": sorted(end_ids),
            "references": sorted(ref_ids),
        }
        if not (record_ids == start_ids == end_ids == ref_ids):
            errors.append("Comment records and start/end/reference anchors do not agree")

        math_objects = len(doc.xpath("//m:oMath", namespaces=NS))
        report["math_objects"] = math_objects
        report["media_parts"] = len(media_names(package))
        report["hard_page_breaks"] = len(
            doc.xpath("//w:br[@w:type='page']", namespaces=NS)
        )
        report["page_break_before"] = len(doc.xpath("//w:pageBreakBefore", namespaces=NS))
        output_orphan_media = set(media_names(package)) - referenced_media(package)
        report["orphan_media"] = sorted(output_orphan_media)

        if base_path is not None:
            with ZipFile(base_path) as base:
                base_names = set(base.namelist())
                changed_base_parts = sorted(
                    name for name in (base_names & names)
                    if digest(base.read(name)) != digest(package.read(name))
                )
                allowed_changed_parts = {
                    "[Content_Types].xml",
                    "word/document.xml",
                    "word/_rels/document.xml.rels",
                    "word/settings.xml",
                    "word/comments.xml",
                    "word/commentsExtended.xml",
                    "word/commentsIds.xml",
                    "word/commentsExtensible.xml",
                    "word/people.xml",
                }
                unexpected_changed_parts = sorted(
                    set(changed_base_parts) - allowed_changed_parts
                )
                report["changed_base_parts"] = changed_base_parts
                report["unexpected_changed_base_parts"] = unexpected_changed_parts
                if unexpected_changed_parts:
                    errors.append(
                        f"Unexpected pre-existing parts changed: {unexpected_changed_parts}"
                    )
                missing_base_parts = sorted(base_names - names)
                if missing_base_parts:
                    errors.append(f"Output dropped base parts: {missing_base_parts[:20]}")
                base_media = media_names(base)
                changed_base_media = [
                    name for name in base_media
                    if name not in names or digest(base.read(name)) != digest(package.read(name))
                ]
                report["changed_base_media"] = changed_base_media
                report["added_media"] = sorted(set(media_names(package)) - set(base_media))
                if changed_base_media:
                    errors.append(f"Pre-existing base media changed: {changed_base_media}")
                inherited_orphan_media = set(base_media) - referenced_media(base)
                new_orphan_media = sorted(output_orphan_media - inherited_orphan_media)
                report["inherited_orphan_media"] = sorted(inherited_orphan_media)
                report["new_orphan_media"] = new_orphan_media
                if inherited_orphan_media:
                    warnings.append(
                        f"Base already contains orphan media: {sorted(inherited_orphan_media)}"
                    )
                if new_orphan_media:
                    errors.append(f"Transplant introduced orphan media: {new_orphan_media}")

                if "word/document.xml" in base_names:
                    base_doc = parse(base.read("word/document.xml"))
                    base_docpr_ids = base_doc.xpath("//wp:docPr/@id", namespaces=NS)
                    inherited_duplicate_docpr_ids = {
                        key for key, value in Counter(base_docpr_ids).items() if value > 1
                    }
                    new_duplicate_docpr_ids = sorted(
                        duplicate_docpr_ids - inherited_duplicate_docpr_ids
                    )
                    report["inherited_duplicate_docpr_ids"] = sorted(
                        inherited_duplicate_docpr_ids
                    )
                    report["new_duplicate_docpr_ids"] = new_duplicate_docpr_ids
                    if inherited_duplicate_docpr_ids:
                        warnings.append(
                            "Base already contains duplicate wp:docPr IDs: "
                            f"{sorted(inherited_duplicate_docpr_ids)}"
                        )
                    if new_duplicate_docpr_ids:
                        errors.append(
                            f"Transplant introduced duplicate wp:docPr IDs: {new_duplicate_docpr_ids}"
                        )
                    base_body = base_doc.find(f"{{{W}}}body")
                    output_body = doc.find(f"{{{W}}}body")
                    if base_body is not None and output_body is not None:
                        output_children = list(output_body)
                        cursor = 0
                        lost_children = []
                        for index, child in enumerate(base_body):
                            skip = any(
                                marker in element_text(child)
                                for marker in allowed_base_text_changes
                            )
                            if (
                                child.tag == f"{{{W}}}p"
                                and child.xpath(
                                    ".//w:fldChar[@w:fldCharType='end']",
                                    namespaces=NS,
                                )
                                and element_text(child) == ""
                            ):
                                skip = True
                            if skip:
                                continue
                            wanted = canonical(child)
                            while (
                                cursor < len(output_children)
                                and canonical(output_children[cursor]) != wanted
                            ):
                                cursor += 1
                            if cursor >= len(output_children):
                                lost_children.append((index, element_text(child)[:120]))
                                break
                            cursor += 1
                        report["lost_or_changed_base_body_children"] = lost_children
                        if lost_children:
                            errors.append(
                                "Base body content changed outside the allowed insertion/"
                                f"bibliography endpoint: {lost_children[:5]}"
                            )
                    base_math = len(base_doc.xpath("//m:oMath", namespaces=NS))
                    report["base_math_objects"] = base_math
                    if math_objects < base_math:
                        errors.append(
                            f"Native equation count decreased from {base_math} to {math_objects}"
                        )
                    if minimum_added_math is not None:
                        actual_added_math = math_objects - base_math
                        report["added_math_objects"] = actual_added_math
                        if actual_added_math < minimum_added_math:
                            errors.append(
                                f"Added native equations {actual_added_math} are below required "
                                f"minimum {minimum_added_math}"
                            )
                    base_revision_ids = set(
                        base_doc.xpath("//*[@w:author and @w:id]/@w:id", namespaces=NS)
                    )
                    if expected_author:
                        new_revisions = [
                            el for el in revisions
                            if el.get(f"{{{W}}}id") not in base_revision_ids
                        ]
                        wrong = Counter(
                            el.get(f"{{{W}}}author") for el in new_revisions
                            if el.get(f"{{{W}}}author") != expected_author
                        )
                        report["new_revision_count"] = len(new_revisions)
                        if not new_revisions:
                            errors.append("No new tracked revisions found relative to base")
                        if wrong:
                            errors.append(
                                f"New revisions not authored by {expected_author!r}: {dict(wrong)}"
                            )
        else:
            if output_orphan_media:
                errors.append(f"Orphan media: {sorted(output_orphan_media)}")
            if duplicate_docpr_ids:
                errors.append(f"Duplicate wp:docPr IDs: {sorted(duplicate_docpr_ids)}")
            if minimum_added_math is not None:
                errors.append("--min-added-math requires --base")
            if expected_author and expected_author not in revision_authors:
                errors.append(f"No tracked revision authored by {expected_author!r}")

    report["ok"] = not errors
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--expected-author")
    parser.add_argument("--require-text", action="append", default=[])
    parser.add_argument(
        "--allow-base-text-change",
        action="append",
        default=[],
        help="Text identifying an explicitly allowed changed direct-body element",
    )
    parser.add_argument(
        "--min-added-math",
        type=int,
        help="Minimum native m:oMath objects added relative to --base",
    )
    parser.add_argument("--max-size-mib", type=float)
    parser.add_argument(
        "--tracked-block-start",
        help="Exact first direct-body text of the inserted tracked block",
    )
    parser.add_argument(
        "--tracked-block-before",
        help="Exact direct-body text immediately after the inserted tracked block",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    report = verify(
        args.docx,
        args.base,
        args.expected_author,
        args.require_text,
        args.allow_base_text_change,
        args.min_added_math,
        args.max_size_mib,
        args.tracked_block_start,
        args.tracked_block_before,
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"DOCX: {report['path']}")
        print("PASS" if report.get("ok") else "FAIL")
        for key in (
            "media_parts",
            "orphan_media",
            "added_media",
            "changed_base_media",
            "changed_base_parts",
            "unexpected_changed_base_parts",
            "math_objects",
            "base_math_objects",
            "added_math_objects",
            "duplicate_docpr_ids",
            "revision_count",
            "new_revision_count",
            "revision_authors",
            "required_text_counts",
        ):
            if key in report:
                print(f"{key}: {report[key]}")
        for warning in report.get("warnings", []):
            print(f"WARNING: {warning}")
        for error in report.get("errors", []):
            print(f"ERROR: {error}")
    raise SystemExit(0 if report.get("ok") else 1)


if __name__ == "__main__":
    main()
