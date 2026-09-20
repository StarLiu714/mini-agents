#!/usr/bin/env python3
"""Transplant an already tracked DOCX body block into an updated DOCX base.

The destination package is copied first. Only the XML parts affected by the
transplant and newly embedded media are updated, so existing media are retained
byte-for-byte instead of being recompressed.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from zipfile import ZipFile

from lxml import etree


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
WP14 = "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {
    "w": W,
    "m": M,
    "r": R,
    "w14": W14,
    "w15": W15,
    "w16cid": W16CID,
    "w16cex": W16CEX,
    "wp": WP,
    "wp14": WP14,
    "ct": CT,
}

COMMENT_PARTS = {
    "word/comments.xml": (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    ),
    "word/commentsExtended.xml": (
        "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml",
    ),
    "word/commentsIds.xml": (
        "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml",
    ),
    "word/commentsExtensible.xml": (
        "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml",
    ),
    "word/people.xml": (
        "http://schemas.microsoft.com/office/2011/relationships/people",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml",
    ),
}


def parse(data: bytes) -> etree._Element:
    return etree.fromstring(data)


def serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone="yes")


def visible_text(element: etree._Element) -> str:
    return "".join(element.itertext()).strip()


def normalize_part(source_part: str, target: str) -> str:
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(source_part), target)
    ).lstrip("/")


def style_maps(styles_xml: bytes) -> tuple[dict[str, str], dict[str, str]]:
    root = parse(styles_xml)
    id_to_name: dict[str, str] = {}
    name_to_id: dict[str, str] = {}
    for style in root.xpath("//w:style", namespaces=NS):
        style_id = style.get(f"{{{W}}}styleId")
        name_element = style.find("w:name", NS)
        if style_id and name_element is not None:
            name = name_element.get(f"{{{W}}}val", "")
            id_to_name[style_id] = name
            name_to_id.setdefault(name.casefold(), style_id)
    return id_to_name, name_to_id


def remap_styles(
    element: etree._Element,
    donor_names: dict[str, str],
    base_ids: dict[str, str],
) -> None:
    for style in element.xpath(
        ".//w:pStyle | .//w:rStyle | .//w:tblStyle | self::w:pStyle | self::w:rStyle | self::w:tblStyle",
        namespaces=NS,
    ):
        old_id = style.get(f"{{{W}}}val", "")
        name = donor_names.get(old_id)
        if not name:
            continue
        new_id = base_ids.get(name.casefold())
        if not new_id:
            raise RuntimeError(f"Destination lacks donor style named {name!r}")
        style.set(f"{{{W}}}val", new_id)


def exact_body_index(body: etree._Element, wanted: str, start: int = 0) -> int:
    hits = [
        index for index, child in enumerate(body)
        if index >= start and visible_text(child) == wanted
    ]
    if len(hits) != 1:
        raise RuntimeError(f"Expected one direct-body match for {wanted!r}, found {hits}")
    return hits[0]


def next_rid(rels: etree._Element) -> int:
    numbers = []
    for rel in rels:
        match = re.fullmatch(r"rId(\d+)", rel.get("Id", ""))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def unique_hex(used: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8].upper()
        if candidate not in used:
            used.add(candidate)
            return candidate


def unique_media_name(used: set[str], suffix: str, counter: int) -> str:
    while True:
        candidate = f"transplanted_{counter:03d}{suffix.lower()}"
        counter += 1
        if candidate not in used:
            used.add(candidate)
            return candidate


def add_content_type_for_extension(
    content_types: etree._Element,
    donor_content_types: etree._Element,
    extension: str,
) -> bool:
    extension = extension.lstrip(".").casefold()
    existing = {
        element.get("Extension", "").casefold()
        for element in content_types.xpath("//ct:Default", namespaces=NS)
    }
    if extension in existing:
        return False
    donor_matches = [
        element for element in donor_content_types.xpath("//ct:Default", namespaces=NS)
        if element.get("Extension", "").casefold() == extension
    ]
    if donor_matches:
        content_types.append(copy.deepcopy(donor_matches[0]))
    else:
        mime = mimetypes.types_map.get(f".{extension}")
        if not mime:
            raise RuntimeError(f"Cannot infer a content type for .{extension}")
        element = etree.Element(f"{{{CT}}}Default")
        element.set("Extension", extension)
        element.set("ContentType", mime)
        content_types.append(element)
    return True


def ensure_part_relationship_and_override(
    part_name: str,
    rels: etree._Element,
    content_types: etree._Element,
    rid_counter: list[int],
) -> None:
    rel_type, content_type = COMMENT_PARTS[part_name]
    target = Path(part_name).name
    if not any(rel.get("Type") == rel_type for rel in rels):
        rel = etree.Element("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship")
        rel.set("Id", f"rId{rid_counter[0]}")
        rid_counter[0] += 1
        rel.set("Type", rel_type)
        rel.set("Target", target)
        rels.append(rel)
    normalized = "/" + part_name
    if not content_types.xpath(
        "//ct:Override[@PartName=$name]", namespaces=NS, name=normalized
    ):
        override = etree.Element(f"{{{CT}}}Override")
        override.set("PartName", normalized)
        override.set("ContentType", content_type)
        content_types.append(override)


def empty_root_from(source_xml: bytes) -> etree._Element:
    root = parse(source_xml)
    for child in list(root):
        root.remove(child)
    return root


def remove_comment_anchors(roots: list[etree._Element]) -> None:
    for root in roots:
        for marker in root.xpath(
            ".//w:commentRangeStart | .//w:commentRangeEnd | .//w:commentReference",
            namespaces=NS,
        ):
            parent = marker.getparent()
            if parent is None:
                continue
            parent.remove(marker)
            if parent.tag == f"{{{W}}}r" and not len(parent) and not visible_text(parent):
                grandparent = parent.getparent()
                if grandparent is not None:
                    grandparent.remove(parent)


def parse_citation_maps(values: list[str]) -> dict[int, int]:
    result: dict[int, int] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Citation map must be OLD=NEW, got {value!r}")
        old, new = value.split("=", 1)
        result[int(old)] = int(new)
    return result


def renumber_superscript_citations(
    roots: list[etree._Element], mapping: dict[int, int]
) -> dict[int, int]:
    hits = {old: 0 for old in mapping}
    for root in roots:
        for run in root.xpath(
            ".//w:r[w:rPr/w:vertAlign[@w:val='superscript']]", namespaces=NS
        ):
            text_nodes = run.xpath(".//w:t", namespaces=NS)
            combined = "".join(node.text or "" for node in text_nodes)
            if not re.fullmatch(r"[\d\s,;\-–—]+", combined or ""):
                continue

            changed = False

            def replace(match: re.Match[str]) -> str:
                nonlocal changed
                old = int(match.group(0))
                if old not in mapping:
                    return match.group(0)
                hits[old] += 1
                changed = True
                return str(mapping[old])

            replaced = re.sub(r"\d+", replace, combined)
            if changed and text_nodes:
                text_nodes[0].text = replaced
                for node in text_nodes[1:]:
                    node.text = ""
    return hits


def find_reference_paragraph(
    body: etree._Element, reference_heading_index: int, number: int
) -> etree._Element:
    pattern = re.compile(rf"^\s*{number}\.\s*")
    matches = [
        child for child in list(body)[reference_heading_index + 1 :]
        if child.tag == f"{{{W}}}p" and pattern.match(visible_text(child))
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one donor bibliography paragraph {number}, found {len(matches)}")
    return matches[0]


def bibliography_max(body: etree._Element) -> tuple[int, int]:
    heading_hits = [
        index for index, child in enumerate(body)
        if visible_text(child).casefold() == "references"
    ]
    if not heading_hits:
        raise RuntimeError("Destination has no direct-body References heading")
    heading = heading_hits[-1]
    values = []
    for child in list(body)[heading + 1 :]:
        match = re.match(r"^\s*(\d+)\.\s*", visible_text(child))
        if match:
            values.append(int(match.group(1)))
    return heading, max(values, default=0)


def renumber_reference(reference: etree._Element, old: int, new: int) -> None:
    for node in reference.xpath(".//w:t", namespaces=NS):
        value = node.text or ""
        changed, count = re.subn(rf"^(\s*){old}\.", rf"\g<1>{new}.", value, count=1)
        if count:
            node.text = changed
            return
    raise RuntimeError(f"Cannot renumber donor bibliography paragraph {old}")


def remove_field_char_runs(reference: etree._Element) -> None:
    for run in reference.xpath("./w:r[.//w:fldChar]", namespaces=NS):
        parent = run.getparent()
        if parent is not None:
            parent.remove(run)


def tracked_block_violations(
    roots: list[etree._Element], *, require_body_structure: bool = True
) -> list[str]:
    """Return content that is not covered by genuine Word revision markup."""
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
        revisions = root.xpath(
            ".//*[@w:author and @w:id] | self::*[@w:author and @w:id]",
            namespaces=NS,
        )
        if not revisions:
            violations.append(f"body child {index} has no revision markup")
            continue
        if require_body_structure and not root_is_inserted and root.tag == f"{{{W}}}p":
            if not root.xpath(
                "./w:pPr/w:rPr/w:ins[@w:author and @w:id] | "
                "./w:pPr/w:rPr/w:moveTo[@w:author and @w:id]",
                namespaces=NS,
            ):
                violations.append(
                    f"body child {index} paragraph mark is not tracked as inserted"
                )
        elif require_body_structure and not root_is_inserted and root.tag == f"{{{W}}}tbl":
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
        elif require_body_structure and not root_is_inserted and root.tag not in {
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
            preview = visible_text(leaf)[:60] or etree.QName(leaf).localname
            violations.append(
                f"body child {index} contains untracked {etree.QName(leaf).localname}: "
                f"{preview!r}"
            )
            if len(violations) >= 20:
                return violations
    return violations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--donor", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-text", required=True, help="Exact first direct-body text in donor block")
    parser.add_argument("--before-text", required=True, help="Exact direct-body text after the block and in base")
    parser.add_argument("--author", required=True)
    parser.add_argument("--copy-reference", action="append", type=int, default=[])
    parser.add_argument("--citation-map", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--no-comments", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    for path in (args.donor, args.base):
        if not path.is_file():
            raise FileNotFoundError(path)
    if args.output.resolve() in {args.donor.resolve(), args.base.resolve()}:
        raise RuntimeError("Output must not overwrite donor or base")
    if args.output.exists() and not args.overwrite:
        raise FileExistsError(f"Output exists; use --overwrite to replace it: {args.output}")

    timestamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manual_citation_map = parse_citation_maps(args.citation_map)

    with ZipFile(args.donor) as donor_zip, ZipFile(args.base) as base_zip:
        donor_doc = parse(donor_zip.read("word/document.xml"))
        base_doc = parse(base_zip.read("word/document.xml"))
        donor_body = donor_doc.find(f"{{{W}}}body")
        base_body = base_doc.find(f"{{{W}}}body")
        if donor_body is None or base_body is None:
            raise RuntimeError("Donor or base lacks w:body")

        donor_start = exact_body_index(donor_body, args.start_text)
        donor_end = exact_body_index(donor_body, args.before_text, donor_start + 1)
        base_before = exact_body_index(base_body, args.before_text)
        if any(visible_text(child) == args.start_text for child in base_body):
            raise RuntimeError("Base already contains the start marker; refusing to duplicate the block")

        block = [copy.deepcopy(child) for child in list(donor_body)[donor_start:donor_end]]
        tracking_violations = tracked_block_violations(block)
        if tracking_violations:
            raise RuntimeError(
                "Donor block is not wholly covered by tracked-change markup; "
                "insert/track it in Word before transplanting. Examples: "
                + "; ".join(tracking_violations[:5])
            )

        donor_style_names, _ = style_maps(donor_zip.read("word/styles.xml"))
        _, base_style_ids = style_maps(base_zip.read("word/styles.xml"))
        for root in block:
            remap_styles(root, donor_style_names, base_style_ids)

        donor_ref_heading, _ = bibliography_max(donor_body)
        base_ref_heading, base_ref_max = bibliography_max(base_body)
        base_reference_endpoints = [
            child for child in list(base_body)[base_ref_heading + 1 :]
            if child.tag == f"{{{W}}}p"
            and child.xpath(".//w:fldChar[@w:fldCharType='end']", namespaces=NS)
            and visible_text(child) == ""
        ]
        copied_refs: list[etree._Element] = []
        auto_map: dict[int, int] = {}
        next_ref = base_ref_max + 1
        for reference_index, old_number in enumerate(args.copy_reference):
            if old_number in manual_citation_map:
                raise RuntimeError(
                    f"Reference {old_number} cannot be both copied and manually mapped"
                )
            reference = copy.deepcopy(
                find_reference_paragraph(donor_body, donor_ref_heading, old_number)
            )
            remap_styles(reference, donor_style_names, base_style_ids)
            auto_map[old_number] = next_ref
            renumber_reference(reference, old_number, next_ref)
            remove_field_char_runs(reference)
            reference_tracking_violations = tracked_block_violations(
                [reference],
                require_body_structure=not (
                    reference_index == 0 and bool(base_reference_endpoints)
                ),
            )
            if reference_tracking_violations:
                raise RuntimeError(
                    f"Donor reference {old_number} is not wholly tracked; "
                    "track it in Word first. Examples: "
                    + "; ".join(reference_tracking_violations[:5])
                )
            copied_refs.append(reference)
            next_ref += 1

        citation_map = {**manual_citation_map, **auto_map}
        citation_hits = renumber_superscript_citations(block, citation_map)

        imported_roots = block + copied_refs

        donor_rels = parse(donor_zip.read("word/_rels/document.xml.rels"))
        base_rels = parse(base_zip.read("word/_rels/document.xml.rels"))
        donor_rel_by_id = {rel.get("Id"): rel for rel in donor_rels}
        rel_counter = [next_rid(base_rels)]
        donor_content_types = parse(donor_zip.read("[Content_Types].xml"))
        base_content_types = parse(base_zip.read("[Content_Types].xml"))
        content_types_changed = False

        referenced_rids = sorted(
            {
                rid
                for root in imported_roots
                for rid in root.xpath(".//@r:id | .//@r:embed | .//@r:link", namespaces=NS)
            }
        )
        base_media = {
            Path(name).name
            for name in base_zip.namelist()
            if name.startswith("word/media/") and not name.endswith("/")
        }
        media_payloads: dict[str, bytes] = {}
        relationship_map: dict[str, str] = {}
        media_counter = 1
        for old_rid in referenced_rids:
            donor_rel = donor_rel_by_id.get(old_rid)
            if donor_rel is None:
                raise RuntimeError(f"Donor relationship {old_rid} is undefined")
            rel_type = donor_rel.get("Type", "")
            mode = donor_rel.get("TargetMode")
            if mode == "External" and rel_type.endswith("/hyperlink"):
                pass
            elif mode == "External":
                raise RuntimeError(
                    f"Refusing external non-hyperlink relationship {old_rid}: {rel_type}"
                )
            elif not rel_type.endswith("/image"):
                raise RuntimeError(
                    f"Unsupported internal relationship in imported block: {old_rid} {rel_type}"
                )

            new_rid = f"rId{rel_counter[0]}"
            rel_counter[0] += 1
            relationship_map[old_rid] = new_rid
            new_rel = copy.deepcopy(donor_rel)
            new_rel.set("Id", new_rid)
            if rel_type.endswith("/image"):
                source_part = normalize_part("word/document.xml", donor_rel.get("Target", ""))
                if source_part not in donor_zip.namelist():
                    raise RuntimeError(f"Donor image part is missing: {source_part}")
                suffix = Path(source_part).suffix
                media_name = unique_media_name(base_media, suffix, media_counter)
                media_counter += 1
                new_rel.set("Target", f"media/{media_name}")
                media_payloads[f"word/media/{media_name}"] = donor_zip.read(source_part)
                content_types_changed |= add_content_type_for_extension(
                    base_content_types, donor_content_types, suffix
                )
            base_rels.append(new_rel)

        for root in imported_roots:
            for element in root.xpath(".//*[@r:id or @r:embed or @r:link]", namespaces=NS):
                for attribute in (f"{{{R}}}id", f"{{{R}}}embed", f"{{{R}}}link"):
                    old_rid = element.get(attribute)
                    if old_rid in relationship_map:
                        element.set(attribute, relationship_map[old_rid])

        comment_roots: dict[str, etree._Element] = {}
        base_names = set(base_zip.namelist())
        donor_names = set(donor_zip.namelist())
        comment_parts_changed = False
        comment_ids = {
            value
            for root in imported_roots
            for value in root.xpath(
                ".//w:commentRangeStart/@w:id | .//w:commentRangeEnd/@w:id | .//w:commentReference/@w:id",
                namespaces=NS,
            )
        }
        if args.no_comments:
            remove_comment_anchors(imported_roots)
            comment_ids.clear()
        elif comment_ids:
            if "word/comments.xml" not in donor_names:
                raise RuntimeError("Imported comment anchors exist but donor has no comments.xml")

            required_comment_parts = ["word/comments.xml"]
            for optional_part in (
                "word/commentsExtended.xml",
                "word/commentsIds.xml",
                "word/commentsExtensible.xml",
                "word/people.xml",
            ):
                if optional_part in donor_names:
                    required_comment_parts.append(optional_part)

            for part in required_comment_parts:
                if part in base_names:
                    comment_roots[part] = parse(base_zip.read(part))
                else:
                    comment_roots[part] = empty_root_from(donor_zip.read(part))
                    ensure_part_relationship_and_override(
                        part, base_rels, base_content_types, rel_counter
                    )
                    content_types_changed = True

            donor_comments = parse(donor_zip.read("word/comments.xml"))
            base_comments = comment_roots["word/comments.xml"]
            used_comment_para_ids = set(base_doc.xpath("//@w14:paraId", namespaces=NS))
            used_comment_para_ids.update(
                base_comments.xpath("//@w14:paraId", namespaces=NS)
            )
            existing_ids = [
                int(value)
                for value in base_comments.xpath("//w:comment/@w:id", namespaces=NS)
                if value.isdigit()
            ]
            next_comment_id = max(existing_ids, default=-1) + 1
            comment_id_map: dict[str, str] = {}
            selected_comment_para_ids: set[str] = set()
            comment_para_id_map: dict[str, str] = {}
            copied_comments: list[etree._Element] = []
            for old_id in sorted(comment_ids, key=lambda value: int(value)):
                matches = donor_comments.xpath(
                    "//w:comment[@w:id=$id]", namespaces=NS, id=old_id
                )
                if len(matches) != 1:
                    raise RuntimeError(f"Cannot uniquely resolve donor comment {old_id}")
                new_id = str(next_comment_id)
                next_comment_id += 1
                comment_id_map[old_id] = new_id
                comment = copy.deepcopy(matches[0])
                comment.set(f"{{{W}}}id", new_id)
                remap_styles(comment, donor_style_names, base_style_ids)
                if comment.xpath(".//@r:id | .//@r:embed | .//@r:link", namespaces=NS):
                    raise RuntimeError(
                        f"Comment {old_id} contains relationships; copy it with Microsoft Word"
                    )
                for paragraph in comment.xpath(".//w:p[@w14:paraId]", namespaces=NS):
                    old_para_id = paragraph.get(f"{{{W14}}}paraId")
                    new_para_id = unique_hex(used_comment_para_ids)
                    comment_para_id_map[old_para_id] = new_para_id
                    paragraph.set(f"{{{W14}}}paraId", new_para_id)
                copied_comments.append(comment)
                selected_comment_para_ids.update(
                    comment_para_id_map.keys()
                )

            # Threaded comments require copying a whole conversation graph. Fail
            # rather than silently orphaning a parent or reply.
            if "word/commentsExtended.xml" in donor_names:
                donor_comments_ex = parse(donor_zip.read("word/commentsExtended.xml"))
                for entry in donor_comments_ex:
                    para_id = entry.get(f"{{{W15}}}paraId")
                    parent = entry.get(f"{{{W15}}}paraIdParent")
                    if para_id in selected_comment_para_ids and parent:
                        raise RuntimeError("Selected block contains a threaded comment reply")
                    if parent in selected_comment_para_ids and para_id not in selected_comment_para_ids:
                        raise RuntimeError("Selected block comment has replies outside the block")

            for root in imported_roots:
                for marker in root.xpath(
                    ".//w:commentRangeStart | .//w:commentRangeEnd | .//w:commentReference",
                    namespaces=NS,
                ):
                    old_id = marker.get(f"{{{W}}}id")
                    if old_id in comment_id_map:
                        marker.set(f"{{{W}}}id", comment_id_map[old_id])
            for comment in copied_comments:
                base_comments.append(comment)

            if "word/commentsExtended.xml" in required_comment_parts:
                donor_part = parse(donor_zip.read("word/commentsExtended.xml"))
                base_part = comment_roots["word/commentsExtended.xml"]
                for para_id in selected_comment_para_ids:
                    for entry in donor_part.xpath(
                        "/*/*[@w15:paraId=$id]", namespaces=NS, id=para_id
                    ):
                        copied = copy.deepcopy(entry)
                        copied.set(
                            f"{{{W15}}}paraId", comment_para_id_map.get(para_id, para_id)
                        )
                        parent = copied.get(f"{{{W15}}}paraIdParent")
                        if parent in comment_para_id_map:
                            copied.set(
                                f"{{{W15}}}paraIdParent", comment_para_id_map[parent]
                            )
                        base_part.append(copied)

            durable_ids: set[str] = set()
            if "word/commentsIds.xml" in required_comment_parts:
                donor_part = parse(donor_zip.read("word/commentsIds.xml"))
                base_part = comment_roots["word/commentsIds.xml"]
                for para_id in selected_comment_para_ids:
                    matches = donor_part.xpath(
                        "/*/*[@w16cid:paraId=$id]", namespaces=NS, id=para_id
                    )
                    for entry in matches:
                        copied = copy.deepcopy(entry)
                        copied.set(
                            f"{{{W16CID}}}paraId", comment_para_id_map.get(para_id, para_id)
                        )
                        durable = copied.get(f"{{{W16CID}}}durableId")
                        if durable:
                            durable_ids.add(durable)
                        base_part.append(copied)

            if "word/commentsExtensible.xml" in required_comment_parts:
                donor_part = parse(donor_zip.read("word/commentsExtensible.xml"))
                base_part = comment_roots["word/commentsExtensible.xml"]
                for durable_id in durable_ids:
                    for entry in donor_part.xpath(
                        "/*/*[@w16cex:durableId=$id]", namespaces=NS, id=durable_id
                    ):
                        base_part.append(copy.deepcopy(entry))

            if "word/people.xml" in required_comment_parts:
                donor_people = parse(donor_zip.read("word/people.xml"))
                base_people = comment_roots["word/people.xml"]
                authors = {comment.get(f"{{{W}}}author") for comment in copied_comments}
                existing_authors = set(
                    base_people.xpath("/*/w15:person/@w15:author", namespaces=NS)
                )
                for author in sorted(authors - existing_authors):
                    matches = donor_people.xpath(
                        "/*/w15:person[@w15:author=$author]", namespaces=NS, author=author
                    )
                    if matches:
                        base_people.append(copy.deepcopy(matches[0]))
            comment_parts_changed = True

        used_para_ids = set(base_doc.xpath("//@w14:paraId", namespaces=NS))
        if "word/comments.xml" in base_names:
            used_para_ids.update(
                parse(base_zip.read("word/comments.xml")).xpath(
                    "//@w14:paraId", namespaces=NS
                )
            )
        if comment_parts_changed:
            used_para_ids.update(
                comment_roots["word/comments.xml"].xpath(
                    "//@w14:paraId", namespaces=NS
                )
            )
        for root in imported_roots:
            for paragraph in root.xpath(
                ".//w:p[@w14:paraId] | self::w:p[@w14:paraId]", namespaces=NS
            ):
                paragraph.set(f"{{{W14}}}paraId", unique_hex(used_para_ids))

        used_bookmark_ids = {
            int(value)
            for value in base_doc.xpath("//w:bookmarkStart/@w:id", namespaces=NS)
            if value.isdigit()
        }
        next_bookmark_id = max(used_bookmark_ids, default=-1) + 1
        bookmark_names = set(base_doc.xpath("//w:bookmarkStart/@w:name", namespaces=NS))
        bookmark_id_map: dict[str, str] = {}
        for root in imported_roots:
            for start in root.xpath(".//w:bookmarkStart", namespaces=NS):
                old_id = start.get(f"{{{W}}}id")
                if old_id not in bookmark_id_map:
                    bookmark_id_map[old_id] = str(next_bookmark_id)
                    next_bookmark_id += 1
                start.set(f"{{{W}}}id", bookmark_id_map[old_id])
                name = start.get(f"{{{W}}}name")
                if name in bookmark_names:
                    name = f"{name}_{uuid.uuid4().hex[:8]}"
                    start.set(f"{{{W}}}name", name)
                if name:
                    bookmark_names.add(name)
            for end in root.xpath(".//w:bookmarkEnd", namespaces=NS):
                old_id = end.get(f"{{{W}}}id")
                if old_id in bookmark_id_map:
                    end.set(f"{{{W}}}id", bookmark_id_map[old_id])

        docpr_ids = [
            int(value) for value in base_doc.xpath("//wp:docPr/@id", namespaces=NS)
            if value.isdigit()
        ]
        next_docpr_id = max(docpr_ids, default=0) + 1
        for root in imported_roots:
            for docpr in root.xpath(".//wp:docPr", namespaces=NS):
                docpr.set("id", str(next_docpr_id))
                next_docpr_id += 1
            for drawing in root.xpath(".//*[@wp14:anchorId]", namespaces=NS):
                drawing.set(f"{{{WP14}}}anchorId", uuid.uuid4().hex[:8].upper())
            for drawing in root.xpath(".//*[@wp14:editId]", namespaces=NS):
                drawing.set(f"{{{WP14}}}editId", uuid.uuid4().hex[:8].upper())

        base_revision_ids = [
            int(value)
            for value in base_doc.xpath("//*[@w:author and @w:id]/@w:id", namespaces=NS)
            if value.isdigit()
        ]
        next_revision_id = max(base_revision_ids, default=-1) + 1
        for root in imported_roots:
            for revision in root.xpath(
                ".//*[@w:author and @w:id] | self::*[@w:author and @w:id]",
                namespaces=NS,
            ):
                revision.set(f"{{{W}}}id", str(next_revision_id))
                revision.set(f"{{{W}}}author", args.author)
                revision.set(f"{{{W}}}date", timestamp)
                next_revision_id += 1

        # Add references at the bibliography endpoint before inserting the main
        # block; body indices used for the main block are earlier and stay valid.
        if copied_refs:
            if base_reference_endpoints:
                endpoint = base_reference_endpoints[-1]
                first = copied_refs[0]
                for child in list(first):
                    if child.tag == f"{{{W}}}pPr":
                        continue
                    endpoint.append(copy.deepcopy(child))
                endpoint_index = list(base_body).index(endpoint)
                remaining = copied_refs[1:]
            else:
                numeric_paragraphs = [
                    child for child in list(base_body)[base_ref_heading + 1 :]
                    if re.match(r"^\s*\d+\.\s*", visible_text(child))
                ]
                if not numeric_paragraphs:
                    raise RuntimeError("Cannot locate destination bibliography endpoint")
                endpoint_index = list(base_body).index(numeric_paragraphs[-1])
                remaining = copied_refs
            for offset, reference in enumerate(remaining, start=1):
                base_body.insert(endpoint_index + offset, reference)

        for offset, child in enumerate(block):
            base_body.insert(base_before + offset, child)

        settings = parse(base_zip.read("word/settings.xml"))
        if not settings.xpath("./w:trackRevisions", namespaces=NS):
            track = etree.Element(f"{{{W}}}trackRevisions")
            default_tab = settings.find("w:defaultTabStop", NS)
            if default_tab is not None:
                settings.insert(list(settings).index(default_tab), track)
            else:
                settings.insert(0, track)

        updated_parts: dict[str, bytes] = {
            "word/document.xml": serialize(base_doc),
            "word/_rels/document.xml.rels": serialize(base_rels),
            "word/settings.xml": serialize(settings),
            **media_payloads,
        }
        if content_types_changed:
            updated_parts["[Content_Types].xml"] = serialize(base_content_types)
        if comment_parts_changed:
            for part, root in comment_roots.items():
                updated_parts[part] = serialize(root)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    staging_handle = tempfile.NamedTemporaryFile(
        prefix=f".{args.output.name}.",
        suffix=".tmp",
        dir=args.output.parent,
        delete=False,
    )
    staging = Path(staging_handle.name)
    staging_handle.close()
    try:
        shutil.copy2(args.base, staging)
        with tempfile.TemporaryDirectory(prefix="docx_transplant_") as temp_name:
            temp = Path(temp_name)
            for part, data in updated_parts.items():
                destination = temp / part
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)

            existing_parts = [part for part in updated_parts if part in base_names]
            if existing_parts:
                subprocess.run(
                    ["zip", "-q", "-d", str(staging.resolve()), *existing_parts],
                    check=True,
                )
            subprocess.run(
                ["zip", "-q", str(staging.resolve()), *updated_parts.keys()],
                cwd=temp,
                check=True,
            )

        with ZipFile(staging) as staged_zip:
            bad_member = staged_zip.testzip()
            if bad_member:
                raise RuntimeError(f"Staged DOCX has a CRC failure: {bad_member}")
            for part, expected in updated_parts.items():
                if staged_zip.read(part) != expected:
                    raise RuntimeError(f"Staged DOCX did not replace part deterministically: {part}")
        os.replace(staging, args.output)
    finally:
        if staging.exists():
            staging.unlink()

    print(f"output: {args.output.resolve()}")
    print(f"body block: {args.start_text!r} -> before {args.before_text!r}")
    print(f"revision author: {args.author}")
    print(f"copied media: {len(media_payloads)}")
    print(f"comments copied: {len(comment_ids)}")
    print(f"citation mapping: {dict(sorted(citation_map.items()))}")
    print(f"citation hits: {dict(sorted(citation_hits.items()))}")


if __name__ == "__main__":
    main()
