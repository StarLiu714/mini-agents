#!/usr/bin/env python3
"""Inspect a DOCX package and show structural anchors relevant to a transplant."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from lxml import etree


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"w": W, "m": M, "wp": WP, "r": R}


def parse(data: bytes) -> etree._Element:
    return etree.fromstring(data)


def text(element: etree._Element) -> str:
    return "".join(element.itertext()).strip().replace("\n", " ")


def style_names(styles_xml: bytes) -> dict[str, str]:
    root = parse(styles_xml)
    result: dict[str, str] = {}
    for style in root.xpath("//w:style", namespaces=NS):
        style_id = style.get(f"{{{W}}}styleId")
        name = style.find("w:name", NS)
        if style_id and name is not None:
            result[style_id] = name.get(f"{{{W}}}val", "")
    return result


def child_style(element: etree._Element, names: dict[str, str]) -> tuple[str | None, str | None]:
    local = etree.QName(element).localname
    if local == "p":
        matches = element.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
    elif local == "tbl":
        matches = element.xpath("./w:tblPr/w:tblStyle/@w:val", namespaces=NS)
    else:
        matches = []
    style_id = matches[0] if matches else None
    return style_id, names.get(style_id or "")


def inspect(path: Path, needles: list[str], window: int) -> dict:
    with ZipFile(path) as package:
        bad = package.testzip()
        doc = parse(package.read("word/document.xml"))
        body = doc.find(f"{{{W}}}body")
        if body is None:
            raise RuntimeError("word/document.xml has no w:body")
        styles = style_names(package.read("word/styles.xml"))
        settings = parse(package.read("word/settings.xml"))
        children = list(body)
        child_texts = [text(child) for child in children]

        comments = []
        if "word/comments.xml" in package.namelist():
            root = parse(package.read("word/comments.xml"))
            for comment in root.xpath("//w:comment", namespaces=NS):
                comments.append(
                    {
                        "id": comment.get(f"{{{W}}}id"),
                        "author": comment.get(f"{{{W}}}author"),
                        "text": text(comment),
                    }
                )

        reference_heading = None
        for index, value in enumerate(child_texts):
            if value.casefold() == "references":
                reference_heading = index
        reference_numbers = []
        if reference_heading is not None:
            for value in child_texts[reference_heading + 1 :]:
                match = re.match(r"^\s*(\d+)\.\s*", value)
                if match:
                    reference_numbers.append(int(match.group(1)))

        rels = parse(package.read("word/_rels/document.xml.rels"))
        external_relationships = [
            {
                "id": rel.get("Id"),
                "type": rel.get("Type"),
                "target": rel.get("Target"),
            }
            for rel in rels
            if rel.get("TargetMode") == "External"
        ]

        revisions = doc.xpath("//*[@w:author and @w:id]", namespaces=NS)
        author_counts = Counter(el.get(f"{{{W}}}author") for el in revisions)

        around = []
        for needle in needles:
            hits = [i for i, value in enumerate(child_texts) if needle.casefold() in value.casefold()]
            windows = []
            for hit in hits:
                rows = []
                for index in range(max(0, hit - window), min(len(children), hit + window + 1)):
                    style_id, style_name = child_style(children[index], styles)
                    rows.append(
                        {
                            "index": index,
                            "type": etree.QName(children[index]).localname,
                            "style_id": style_id,
                            "style_name": style_name,
                            "text": child_texts[index][:300],
                        }
                    )
                windows.append({"hit": hit, "rows": rows})
            around.append({"needle": needle, "hits": hits, "windows": windows})

        media = [
            name for name in package.namelist()
            if name.startswith("word/media/") and not name.endswith("/")
        ]
        return {
            "path": str(path.resolve()),
            "size_bytes": path.stat().st_size,
            "zip_error": bad,
            "body_children": len(children),
            "tables": len(doc.xpath("//w:tbl", namespaces=NS)),
            "inline_drawings": len(doc.xpath("//wp:inline", namespaces=NS)),
            "anchored_drawings": len(doc.xpath("//wp:anchor", namespaces=NS)),
            "media_parts": len(media),
            "media_bytes_uncompressed": sum(package.getinfo(name).file_size for name in media),
            "math_objects": len(doc.xpath("//m:oMath", namespaces=NS)),
            "math_paragraphs": len(doc.xpath("//m:oMathPara", namespaces=NS)),
            "revision_count": len(revisions),
            "revision_authors": dict(author_counts),
            "track_revisions_enabled": bool(settings.xpath("./w:trackRevisions", namespaces=NS)),
            "comments": comments,
            "comment_anchor_ids": sorted(set(doc.xpath("//w:commentReference/@w:id", namespaces=NS))),
            "reference_heading_index": reference_heading,
            "reference_max": max(reference_numbers, default=None),
            "external_relationships": external_relationships,
            "around": around,
        }


def print_human(result: dict) -> None:
    print(result["path"])
    print(
        "size={:.2f} MiB body_children={} tables={} media={} equations={} revisions={}".format(
            result["size_bytes"] / 2**20,
            result["body_children"],
            result["tables"],
            result["media_parts"],
            result["math_objects"],
            result["revision_count"],
        )
    )
    print(
        f"trackRevisions={result['track_revisions_enabled']} "
        f"revision_authors={result['revision_authors']} comments={len(result['comments'])} "
        f"reference_max={result['reference_max']}"
    )
    if result["external_relationships"]:
        print("external relationships:")
        for rel in result["external_relationships"]:
            print(f"  {rel['id']} {rel['type']} -> {rel['target']}")
    for query in result["around"]:
        print(f"\n[{query['needle']}] hits={query['hits']}")
        for match in query["windows"]:
            print(f"  -- hit {match['hit']} --")
            for row in match["rows"]:
                print(
                    f"  {row['index']:5d} {row['type']:7s} "
                    f"style={row['style_name'] or row['style_id'] or '-'}  {row['text']}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--around", action="append", default=[], help="Text to locate among direct body children")
    parser.add_argument("--window", type=int, default=5)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    result = inspect(args.docx, args.around, args.window)
    if args.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_human(result)


if __name__ == "__main__":
    main()
