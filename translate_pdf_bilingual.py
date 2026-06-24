#!/usr/bin/env python3
"""Translate a PDF into bilingual Markdown with an OpenAI-compatible API.

Default workflow:
  python3 translate_pdf_bilingual.py mpa.pdf -o mpa.bilingual.md

The script reads API_key.json by default:
  {
    "OPENROUTER_API_KEY": "...",
    "OPENROUTER_BASE_URL": "https://.../v1",
    "MEMENTO_MINI_MODEL": "DeepSeek-V4-Flash"
  }

Environment variables with the same names override API_key.json values. Legacy
KEY=VALUE config files are still accepted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def configure_utf8_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class Block:
    block_id: str
    page: int
    original: str
    translation: str = ""


def parse_config_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    raw_text = path.read_text(encoding="utf-8")
    stripped = raw_text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON config in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SystemExit(f"Invalid JSON config in {path}: expected an object")
        return {str(key): str(value) for key, value in data.items() if value is not None}

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_config(args: argparse.Namespace) -> Dict[str, str]:
    file_values = parse_config_file(Path(args.api_file))

    def pick(name: str, default: Optional[str] = None) -> Optional[str]:
        return os.environ.get(name) or file_values.get(name) or default

    api_key = args.api_key or pick("OPENROUTER_API_KEY")
    base_url = args.base_url or pick("OPENROUTER_BASE_URL")
    model = args.model or pick("OPENROUTER_MODEL") or pick("MEMENTO_MINI_MODEL")

    missing = []
    if not api_key:
        missing.append("OPENROUTER_API_KEY")
    if not base_url:
        missing.append("OPENROUTER_BASE_URL")
    if not model:
        missing.append("OPENROUTER_MODEL or MEMENTO_MINI_MODEL")
    if missing:
        raise SystemExit("Missing API config: " + ", ".join(missing))

    return {
        "api_key": str(api_key),
        "base_url": str(base_url).rstrip("/"),
        "model": str(model),
    }


def require_pdftotext() -> None:
    if shutil.which("pdftotext") is None:
        raise SystemExit(
            "pdftotext is required. Install poppler, for example: brew install poppler"
        )


def extract_pdf_text(pdf_path: Path) -> List[str]:
    require_pdftotext()
    proc = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "pdftotext failed")
    pages = proc.stdout.split("\f")
    return [page.rstrip() for page in pages if page.strip()]


def fix_common_pdf_artifacts(text: str) -> str:
    replacements = {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\u2010": "-",
        "\u2011": "-",
        "\u2212": "-",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = text.replace("https : //", "https://")
    text = text.replace("http : //", "http://")
    return text


def strip_page_number(block: str, page_no: int) -> str:
    lines = [line.rstrip() for line in block.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == str(page_no):
        lines.pop()
    return "\n".join(lines).strip()


def join_wrapped_lines(block: str) -> str:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if not lines:
        return ""

    joined = ""
    for line in lines:
        if not joined:
            joined = line
            continue
        if re.search(r"[A-Za-z]-$", joined) and re.match(r"^[a-z]", line):
            joined = joined[:-1] + line
        else:
            joined += " " + line

    joined = fix_common_pdf_artifacts(joined)
    joined = re.sub(r"\s+", " ", joined)
    joined = re.sub(r"\s+([,.;:?!%\]\)])", r"\1", joined)
    joined = re.sub(r"([\[\(])\s+", r"\1", joined)
    joined = re.sub(r"\b([Ff]ig)\.\s+", r"\1. ", joined)
    return joined.strip()


def page_to_blocks(page_text: str, page_no: int) -> List[str]:
    page_text = fix_common_pdf_artifacts(page_text)
    raw_blocks = re.split(r"\n\s*\n+", page_text)
    blocks: List[str] = []
    for raw_block in raw_blocks:
        stripped = strip_page_number(raw_block, page_no)
        if not stripped:
            continue
        block = join_wrapped_lines(stripped)
        if not block:
            continue
        # Drop isolated page headers/footers that are usually not document content.
        if block == str(page_no):
            continue
        blocks.append(block)
    return blocks


def extract_blocks(pdf_path: Path) -> List[Block]:
    pages = extract_pdf_text(pdf_path)
    blocks: List[Block] = []
    for page_no, page in enumerate(pages, start=1):
        page_blocks = page_to_blocks(page, page_no)
        for idx, text in enumerate(page_blocks, start=1):
            blocks.append(Block(block_id=f"p{page_no:03d}-b{idx:03d}", page=page_no, original=text))
    return merge_cross_page_continuations(blocks)


def looks_like_cross_page_continuation(previous: str, current: str) -> bool:
    if not previous or not current:
        return False
    if looks_like_display_block(previous) or looks_like_display_block(current):
        return False
    if re.search(r"[.!?。！？][\"')\]]?$", previous):
        return False
    if re.match(r"^(and|or|but|where|whereas|which|that|to|of|in|for|with|from|by)\b", current):
        return True
    if re.match(r"^[a-z]", current):
        return True
    return previous.endswith((",", ";", ":", "-", "–"))


def looks_like_display_block(text: str) -> bool:
    if re.match(r"^(Fig\.|Figure|Table)\b", text):
        return True
    if re.match(r"^Category\s+N\s+", text):
        return True
    if len(re.findall(r"\bimproved\b", text)) >= 3:
        return True
    if re.search(r"\brandom \(ID\)\b.*\bscaffold \(OOD\)\b", text):
        return True
    return False


def merge_cross_page_continuations(blocks: List[Block]) -> List[Block]:
    merged: List[Block] = []
    for block in blocks:
        if (
            merged
            and block.page == merged[-1].page + 1
            and looks_like_cross_page_continuation(merged[-1].original, block.original)
        ):
            merged[-1].original = join_wrapped_lines(merged[-1].original + " " + block.original)
            continue
        merged.append(block)
    return merged


def cache_key(block: Block, model: str, source_lang: str, target_lang: str) -> str:
    payload = json.dumps(
        {
            "model": model,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "text": block.original,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_cache(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    cache: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = item.get("key")
        translation = item.get("translation")
        if key and translation:
            cache[str(key)] = str(translation)
    return cache


def append_cache(path: Path, records: Iterable[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def chunk_blocks(blocks: List[Block], max_chars: int) -> List[List[Block]]:
    chunks: List[List[Block]] = []
    current: List[Block] = []
    current_chars = 0
    for block in blocks:
        size = len(block.original)
        if current and current_chars + size > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(block)
        current_chars += size
    if current:
        chunks.append(current)
    return chunks


def strip_json_fence(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    start = content.find("[")
    end = content.rfind("]")
    if start != -1 and end != -1 and end > start:
        content = content[start : end + 1]
    return content.strip()


def parse_translation_response(content: str) -> Dict[str, str]:
    cleaned = strip_json_fence(content)
    data = json.loads(cleaned)
    if isinstance(data, dict):
        data = data.get("translations", [])
    if not isinstance(data, list):
        raise ValueError("translation response is not a JSON array")

    result: Dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        block_id = item.get("id")
        translation = item.get("translation") or item.get("zh") or item.get("text")
        if block_id and translation:
            result[str(block_id)] = str(translation).strip()
    return result


def api_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if len(body) > 600:
        body = body[:600] + "..."
    return f"HTTP {exc.code}: {body}"


def translate_batch(
    batch: List[Block],
    *,
    api_key: str,
    base_url: str,
    model: str,
    source_lang: str,
    target_lang: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    retries: int,
) -> Dict[str, str]:
    endpoint = f"{base_url}/chat/completions"
    entries = [{"id": block.block_id, "text": block.original} for block in batch]
    system_prompt = (
        "You are a precise academic translator. Translate from "
        f"{source_lang} to {target_lang}. Keep technical terms, model names, "
        "dataset names, units, equations, citations, URLs, and reference numbers accurate. "
        "Do not summarize. Return only valid JSON."
    )
    user_prompt = (
        "Translate each item in this JSON array. Return a JSON array with the same ids; "
        "each item must be {\"id\": string, \"translation\": string}. "
        "Keep paragraph-level meaning and Markdown-safe text.\n\n"
        + json.dumps(entries, ensure_ascii=False)
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Optional[str] = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            result = parse_translation_response(content)
            missing = [block.block_id for block in batch if block.block_id not in result]
            if missing:
                raise ValueError("missing translations for: " + ", ".join(missing[:8]))
            return result
        except urllib.error.HTTPError as exc:
            last_error = api_error_message(exc)
        except Exception as exc:
            last_error = str(exc)

        if attempt < retries:
            time.sleep(min(30, 2 ** attempt))

    raise RuntimeError(f"translation failed after {retries} attempts: {last_error}")


def markdown_escape_inline(text: str) -> str:
    return text.replace("|", "\\|")


def write_markdown(
    path: Path,
    *,
    pdf_path: Path,
    blocks: List[Block],
    model: str,
    source_lang: str,
    target_lang: str,
) -> None:
    generated = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    lines: List[str] = [
        f"# {pdf_path.stem} bilingual machine translation",
        "",
        f"- Source: `{pdf_path.name}`",
        f"- Generated: `{generated}`",
        f"- Model: `{model}`",
        f"- Direction: `{source_lang}` -> `{target_lang}`",
        "",
    ]
    current_page = None
    for block in blocks:
        if block.page != current_page:
            current_page = block.page
            lines.extend([f"## Page {current_page}", ""])
        lines.extend(
            [
                f'<a id="{block.block_id}"></a>',
                "",
                f"**Original ({markdown_escape_inline(block.block_id)})**",
                "",
                block.original,
                "",
                "**中文翻译**",
                "",
                block.translation,
                "",
                "---",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate PDF text into bilingual Markdown using a DeepSeek/OpenAI-compatible API."
    )
    parser.add_argument("pdf", help="Input PDF path.")
    parser.add_argument("-o", "--out", help="Output Markdown path.")
    parser.add_argument("--api-file", default="API_key.json", help="JSON API config file.")
    parser.add_argument("--api-key", help="API key. Defaults to API_key.json or env.")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL. Defaults to API_key.json or env.")
    parser.add_argument("--model", help="Model name. Defaults to OPENROUTER_MODEL or MEMENTO_MINI_MODEL.")
    parser.add_argument("--source-lang", default="English", help="Source language.")
    parser.add_argument("--target-lang", default="Simplified Chinese", help="Target language.")
    parser.add_argument("--max-chars", type=int, default=4500, help="Approx source characters per API call.")
    parser.add_argument("--max-tokens", type=int, default=8192, help="Max completion tokens per API call.")
    parser.add_argument("--temperature", type=float, default=0.1, help="Translation temperature.")
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=4, help="Retries per API call.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between API calls.")
    parser.add_argument("--cache-dir", default=".translation_cache", help="Cache directory.")
    parser.add_argument("--no-cache", action="store_true", help="Disable JSONL translation cache.")
    parser.add_argument("--dry-run", action="store_true", help="Only extract PDF text and write untranslated Markdown.")
    return parser


def main() -> int:
    configure_utf8_stdio()
    args = build_arg_parser().parse_args()
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")
    out_path = Path(args.out) if args.out else pdf_path.with_suffix(".bilingual.md")

    config = get_config(args)
    model = config["model"]

    print(f"Extracting text from {pdf_path} ...", file=sys.stderr)
    blocks = extract_blocks(pdf_path)
    if not blocks:
        raise SystemExit("No extractable text found. This PDF may need OCR.")
    print(f"Extracted {len(blocks)} text blocks.", file=sys.stderr)

    cache_path = Path(args.cache_dir) / f"{pdf_path.stem}.{hashlib.sha1(model.encode()).hexdigest()[:10]}.jsonl"
    cache = {} if args.no_cache else load_cache(cache_path)

    pending: List[Block] = []
    for block in blocks:
        key = cache_key(block, model, args.source_lang, args.target_lang)
        if key in cache:
            block.translation = cache[key]
        else:
            pending.append(block)

    if args.dry_run:
        for block in pending:
            block.translation = "[DRY RUN: not translated]"
        write_markdown(
            out_path,
            pdf_path=pdf_path,
            blocks=blocks,
            model=model,
            source_lang=args.source_lang,
            target_lang=args.target_lang,
        )
        print(f"Wrote {out_path}", file=sys.stderr)
        return 0

    chunks = chunk_blocks(pending, args.max_chars)
    if pending:
        print(f"Translating {len(pending)} blocks in {len(chunks)} API calls.", file=sys.stderr)
    else:
        print("All blocks loaded from cache.", file=sys.stderr)

    for idx, chunk in enumerate(chunks, start=1):
        char_count = sum(len(block.original) for block in chunk)
        print(f"API call {idx}/{len(chunks)}: {len(chunk)} blocks, {char_count} chars.", file=sys.stderr)
        translations = translate_batch(
            chunk,
            api_key=config["api_key"],
            base_url=config["base_url"],
            model=model,
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            retries=args.retries,
        )
        records: List[Dict[str, str]] = []
        for block in chunk:
            translation = translations[block.block_id]
            block.translation = translation
            records.append(
                {
                    "key": cache_key(block, model, args.source_lang, args.target_lang),
                    "id": block.block_id,
                    "model": model,
                    "translation": translation,
                }
            )
        if not args.no_cache:
            append_cache(cache_path, records)
        if args.sleep:
            time.sleep(args.sleep)

    write_markdown(
        out_path,
        pdf_path=pdf_path,
        blocks=blocks,
        model=model,
        source_lang=args.source_lang,
        target_lang=args.target_lang,
    )
    print(f"Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
