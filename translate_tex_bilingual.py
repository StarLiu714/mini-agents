#!/usr/bin/env python3
"""Translate LaTeX article text into Chinese side-by-side paragraphs.

The script keeps the original .tex files unchanged and writes *_zhcn.tex files.
It skips LaTeX files that look like macros, tables, figures, or style helpers.

Default workflow:
  python3 translate_tex_bilingual.py Literature_Review

API config is read from API_key.json and then environment variables:
  {
    "DEEPSEEK_API_KEY": "...",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "DEEPSEEK_MODEL": "deepseek-chat"
  }

OpenRouter-style names from translate_pdf_bilingual.py are also accepted as a
fallback: OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL, and
MEMENTO_MINI_MODEL. Legacy KEY=VALUE config files are still accepted if passed
explicitly with --api-file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


SKIP_ENVS = {
    "algorithm",
    "algorithmic",
    "align",
    "align*",
    "array",
    "displaymath",
    "equation",
    "equation*",
    "figure",
    "figure*",
    "flalign",
    "flalign*",
    "gather",
    "gather*",
    "lstlisting",
    "multline",
    "multline*",
    "picture",
    "pmatrix",
    "smallmatrix",
    "split",
    "subequations",
    "table",
    "table*",
    "tabular",
    "tabular*",
    "tikzpicture",
    "verbatim",
    "vmatrix",
}

FORMAT_HINTS = {
    "macro",
    "macros",
    "math_commands",
    "tables",
    "table",
    "params",
    "param",
    "overview_tex",
    "multizoo_tex",
}


@dataclass
class TexBlock:
    block_id: str
    chunk_index: int
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

    api_key = (
        args.api_key
        or pick("DEEPSEEK_API_KEY")
        or pick("OPENROUTER_API_KEY")
        or pick("OPENAI_API_KEY")
    )
    base_url = (
        args.base_url
        or pick("DEEPSEEK_BASE_URL")
        or pick("OPENROUTER_BASE_URL")
        or pick("OPENAI_BASE_URL")
        or "https://api.deepseek.com"
    )
    model = (
        args.model
        or pick("DEEPSEEK_MODEL")
        or pick("OPENROUTER_MODEL")
        or pick("MEMENTO_MINI_MODEL")
        or "deepseek-chat"
    )

    if not api_key:
        raise SystemExit(
            "Missing API config: set DEEPSEEK_API_KEY in env or API_key.json "
            "(OPENROUTER_API_KEY is also accepted)."
        )

    return {
        "api_key": str(api_key),
        "base_url": str(base_url).rstrip("/"),
        "model": str(model),
    }


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
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Some models return LaTeX commands such as \cite inside JSON strings
        # without doubling the backslash. That is invalid JSON but easy to
        # repair without changing valid JSON escapes.
        cleaned = re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", cleaned)
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
            result[str(block_id)] = clean_translation(str(translation))
    return result


def clean_translation(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:tex|latex)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^\s*\\\\\s*", "", text)
    return text.strip()


def api_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if len(body) > 600:
        body = body[:600] + "..."
    return f"HTTP {exc.code}: {body}"


def translate_batch(
    batch: List[TexBlock],
    *,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    retries: int,
) -> Dict[str, str]:
    endpoint = f"{base_url}/chat/completions"
    entries = [{"id": block.block_id, "text": block.original.strip()} for block in batch]
    system_prompt = (
        "You are a precise academic translator. Translate English LaTeX article "
        "paragraphs into Simplified Chinese. Keep LaTeX commands, citations, "
        "references, math expressions, URLs, labels, dataset names, model names, "
        "and units unchanged. Translate only natural-language prose. Do not add "
        "explanations, summaries, Markdown fences, or LaTeX line-break commands. "
        "Return only valid JSON."
    )
    user_prompt = (
        "Translate each item in this JSON array. Return a JSON array with the "
        "same ids; each item must be {\"id\": string, \"translation\": string}. "
        "The translation should be Chinese prose suitable to paste immediately "
        "after the original LaTeX paragraph.\n\n"
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
            time.sleep(min(30, 2**attempt))

    raise RuntimeError(f"translation failed after {retries} attempts: {last_error}")


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


def cache_key(block: TexBlock, model: str) -> str:
    payload = json.dumps(
        {"model": model, "text": block.original.strip(), "target": "zhcn"},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chunk_blocks(blocks: List[TexBlock], max_chars: int) -> List[List[TexBlock]]:
    chunks: List[List[TexBlock]] = []
    current: List[TexBlock] = []
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


def find_tex_files(paths: Sequence[str]) -> List[Path]:
    files: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.tex")))
        elif path.suffix == ".tex":
            files.append(path)
    return [path for path in files if not path.name.endswith("_zhcn.tex")]


def strip_disabled_conditionals(text: str) -> str:
    """Remove disabled manuscript blocks such as \iffalse ... \fi.

    LaTeX conditionals can nest, so this uses a small token scanner instead of
    a non-greedy regex. It only starts removal at \iffalse; nested \if... tokens
    are counted until the matching \fi.
    """
    token_re = re.compile(r"\\(?:iffalse|if[a-zA-Z@]*|fi)\b")
    output: List[str] = []
    pos = 0

    while True:
        match = token_re.search(text, pos)
        if match is None:
            output.append(text[pos:])
            break
        if match.group(0) != r"\iffalse":
            output.append(text[pos : match.end()])
            pos = match.end()
            continue

        output.append(text[pos : match.start()])
        depth = 1
        scan_pos = match.end()
        while depth:
            nested = token_re.search(text, scan_pos)
            if nested is None:
                pos = len(text)
                break
            token = nested.group(0)
            if token == r"\fi":
                depth -= 1
            elif token.startswith(r"\if"):
                depth += 1
            scan_pos = nested.end()
        else:
            pos = scan_pos

    cleaned_lines = []
    dead_comment_re = re.compile(r"^\s*%.*\b(?:TODO|FIXME|XXX|iffalse|\\fi)\b", re.IGNORECASE)
    for line in "".join(output).splitlines(keepends=True):
        if dead_comment_re.search(line):
            continue
        cleaned_lines.append(line)
    return "".join(cleaned_lines)


def dequote_tex_path(raw_path: str) -> Tuple[str, str, str]:
    stripped = raw_path.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[0], stripped[1:-1], stripped[-1]
    return "", stripped, ""


def translated_input_target_exists(
    tex_path: Path,
    input_path: str,
    translatable_sources: Optional[Set[Path]],
) -> bool:
    candidate = Path(input_path)
    if not candidate.suffix:
        candidate = candidate.with_suffix(".tex")
    if not candidate.is_absolute():
        candidate = tex_path.parent / candidate
    candidate = candidate.resolve()
    translated = candidate.with_name(f"{candidate.stem}_zhcn{candidate.suffix}")
    if translated.exists():
        return True
    if translatable_sources and candidate in translatable_sources:
        return True
    return False


def add_zhcn_suffix_to_tex_path(path_text: str) -> str:
    quote_l, inner, quote_r = dequote_tex_path(path_text)
    if inner.endswith("_zhcn") or inner.endswith("_zhcn.tex"):
        return path_text

    suffix = ".tex" if inner.endswith(".tex") else ""
    stem = inner[: -len(suffix)] if suffix else inner
    return f"{quote_l}{stem}_zhcn{suffix}{quote_r}"


def rewrite_translated_inputs(
    text: str,
    tex_path: Path,
    translatable_sources: Optional[Set[Path]],
) -> str:
    def replace(match: re.Match[str]) -> str:
        command, body = match.group(1), match.group(2)
        _, inner, _ = dequote_tex_path(body)
        if translated_input_target_exists(tex_path, inner, translatable_sources):
            return f"\\{command}" + "{" + add_zhcn_suffix_to_tex_path(body) + "}"
        return match.group(0)

    return re.sub(r"\\(input|include)\s*\{([^}]+)\}", replace, text)


def is_structural_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("%"):
        return True
    if re.match(r"\\(?:begin|end)\{[^}]+\}\s*(?:%.*)?$", stripped):
        return True
    if re.match(
        r"\\(?:documentclass|usepackage|PassOptionsToPackage|input|include|bibliography|bibliographystyle|label|ref|vspace|hspace|maketitle|appendix|printAffiliationsAndNotice)\b",
        stripped,
    ):
        return True
    if re.match(r"\\(?:section|subsection|subsubsection|paragraph|title|author|date)\*?\{", stripped):
        return True
    return False


def split_latex_chunks(text: str) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []

    def flush() -> None:
        if current:
            chunks.append("".join(current))
            current.clear()

    for line in text.splitlines(keepends=True):
        if not line.strip():
            flush()
            chunks.append(line)
            continue
        if is_structural_line(line):
            flush()
            chunks.append(line)
            continue
        current.append(line)
    flush()
    return chunks


def update_env_stack(chunk: str, stack: List[str]) -> None:
    for match in re.finditer(r"\\(begin|end)\{([^}]+)\}", chunk):
        action, env = match.group(1), match.group(2)
        if action == "begin":
            stack.append(env)
            continue
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] == env:
                del stack[index:]
                break


def remove_latex_noise(text: str) -> str:
    text = re.sub(r"%.*", " ", text)
    text = re.sub(r"\$[^$]*\$", " MATH ", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})?", " CMD ", text)
    text = re.sub(r"[{}\\_^&~#]", " ", text)
    return text


def looks_like_prose(chunk: str, inside_skip_env: bool) -> bool:
    if inside_skip_env:
        return False
    stripped = chunk.strip()
    if not stripped:
        return False
    if re.search(r"[\u4e00-\u9fff]", stripped):
        return False
    if "\\begin{" in stripped or "\\end{" in stripped:
        return False
    if re.match(r"\\(section|subsection|subsubsection|paragraph|title|author)\*?\{", stripped):
        return False
    if stripped.count("&") >= 2:
        return False

    prose = remove_latex_noise(stripped)
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", prose)
    if len(words) < 8:
        return False
    if sum(len(word) for word in words) < 40:
        return False

    command_lines = sum(1 for line in stripped.splitlines() if line.strip().startswith("\\"))
    total_lines = max(1, len(stripped.splitlines()))
    if command_lines / total_lines > 0.6 and not stripped.lstrip().startswith("\\item"):
        return False
    return True


def extract_blocks(tex_path: Path, text: str) -> Tuple[List[str], List[TexBlock]]:
    chunks = split_latex_chunks(text)
    blocks: List[TexBlock] = []
    env_stack: List[str] = []
    file_hash = hashlib.sha1(str(tex_path).encode("utf-8")).hexdigest()[:8]

    for index, chunk in enumerate(chunks):
        inside_skip_env = any(env in SKIP_ENVS for env in env_stack)
        if looks_like_prose(chunk, inside_skip_env):
            block_id = f"{file_hash}-{len(blocks) + 1:04d}"
            blocks.append(TexBlock(block_id=block_id, chunk_index=index, original=chunk))
        update_env_stack(chunk, env_stack)
    return chunks, blocks


def insert_xecjk(text: str) -> str:
    if "\\usepackage{xeCJK}" in text or "\\usepackage[AutoFakeBold]{xeCJK}" in text:
        return text
    begin_match = re.search(r"\\begin\{document\}", text)
    search_end = begin_match.start() if begin_match else len(text)
    preamble = text[:search_end]
    if "\\documentclass" not in preamble:
        return text

    package_matches = list(re.finditer(r"^[ \t]*\\usepackage(?:\[[^\]]*\])?\{[^}]+\}.*\n?", preamble, re.MULTILINE))
    if package_matches:
        insert_at = package_matches[-1].end()
    else:
        doc_match = re.search(r"^[ \t]*\\documentclass(?:\[[^\]]*\])?\{[^}]+\}.*\n?", preamble, re.MULTILINE)
        if not doc_match:
            return text
        insert_at = doc_match.end()
    return text[:insert_at] + "\\usepackage{xeCJK}\n" + text[insert_at:]


def format_bilingual_chunk(original: str, translation: str) -> str:
    if not translation:
        return original
    suffix = "" if original.endswith("\n") else "\n"
    return original + suffix + "\\\\\n" + translation.strip() + "\n"


def rebuild_text(chunks: List[str], blocks: List[TexBlock]) -> str:
    by_index = {block.chunk_index: block for block in blocks}
    rebuilt: List[str] = []
    for index, chunk in enumerate(chunks):
        block = by_index.get(index)
        if block is None:
            rebuilt.append(chunk)
        else:
            rebuilt.append(format_bilingual_chunk(chunk, block.translation))
    return "".join(rebuilt)


def probably_format_file(path: Path, blocks: List[TexBlock]) -> bool:
    stem = path.stem.lower()
    if any(hint == stem or hint in stem for hint in FORMAT_HINTS):
        return True
    if not blocks:
        return True
    return False


def collect_translatable_sources(files: Sequence[Path], min_paragraphs: int) -> Set[Path]:
    sources: Set[Path] = set()
    for tex_path in files:
        try:
            text = strip_disabled_conditionals(tex_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            continue
        _, blocks = extract_blocks(tex_path, text)
        if not probably_format_file(tex_path, blocks) and len(blocks) >= min_paragraphs:
            sources.add(tex_path.resolve())
    return sources


def process_file(
    tex_path: Path,
    *,
    config: Optional[Dict[str, str]],
    args: argparse.Namespace,
    translatable_sources: Optional[Set[Path]] = None,
) -> Tuple[str, int, Path]:
    text = strip_disabled_conditionals(tex_path.read_text(encoding="utf-8"))
    chunks, blocks = extract_blocks(tex_path, text)
    out_path = tex_path.with_name(f"{tex_path.stem}_zhcn.tex")

    if probably_format_file(tex_path, blocks) or len(blocks) < args.min_paragraphs:
        return "skip", len(blocks), out_path
    if args.dry_run:
        return "would_translate", len(blocks), out_path
    if out_path.exists() and not args.force:
        existing = out_path.read_text(encoding="utf-8")
        updated = strip_disabled_conditionals(existing)
        updated = rewrite_translated_inputs(updated, tex_path, translatable_sources)
        updated = insert_xecjk(updated)
        if updated != existing:
            out_path.write_text(updated, encoding="utf-8")
            return "updated", len(blocks), out_path
        return "exists", len(blocks), out_path
    if config is None:
        raise RuntimeError("internal error: missing API config")

    model = config["model"]
    cache_path = Path(args.cache_dir) / f"{tex_path.stem}.{hashlib.sha1(model.encode()).hexdigest()[:10]}.jsonl"
    cache = {} if args.no_cache else load_cache(cache_path)

    pending: List[TexBlock] = []
    for block in blocks:
        key = cache_key(block, model)
        if key in cache:
            block.translation = cache[key]
        else:
            pending.append(block)

    batches = chunk_blocks(pending, args.max_chars)
    if pending:
        print(
            f"{tex_path}: translating {len(pending)} paragraphs in {len(batches)} API calls.",
            file=sys.stderr,
        )
    else:
        print(f"{tex_path}: all {len(blocks)} paragraphs loaded from cache.", file=sys.stderr)

    for batch_index, batch in enumerate(batches, start=1):
        char_count = sum(len(block.original) for block in batch)
        print(
            f"  API call {batch_index}/{len(batches)}: {len(batch)} paragraphs, {char_count} chars.",
            file=sys.stderr,
        )
        translations = translate_batch(
            batch,
            api_key=config["api_key"],
            base_url=config["base_url"],
            model=model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            retries=args.retries,
        )
        records: List[Dict[str, str]] = []
        for block in batch:
            block.translation = translations[block.block_id]
            records.append(
                {
                    "key": cache_key(block, model),
                    "id": block.block_id,
                    "model": model,
                    "translation": block.translation,
                }
            )
        if not args.no_cache:
            append_cache(cache_path, records)
        if args.sleep:
            time.sleep(args.sleep)

    output = rebuild_text(chunks, blocks)
    output = rewrite_translated_inputs(output, tex_path, translatable_sources)
    output = insert_xecjk(output)
    out_path.write_text(output, encoding="utf-8")
    return "translated", len(blocks), out_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate article-content .tex files into *_zhcn.tex using the DeepSeek API."
    )
    parser.add_argument("paths", nargs="*", default=["Literature_Review"], help="Input .tex files or directories.")
    parser.add_argument("--api-file", default="API_key.json", help="JSON API config file.")
    parser.add_argument("--api-key", help="API key. Defaults to API_key.json or env.")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL. Defaults to DeepSeek.")
    parser.add_argument("--model", help="Model name. Defaults to deepseek-chat.")
    parser.add_argument("--max-chars", type=int, default=5000, help="Approx source characters per API call.")
    parser.add_argument("--max-tokens", type=int, default=8192, help="Max completion tokens per API call.")
    parser.add_argument("--temperature", type=float, default=0.1, help="Translation temperature.")
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=4, help="Retries per API call.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between API calls.")
    parser.add_argument("--cache-dir", default=".translation_cache_tex", help="JSONL translation cache directory.")
    parser.add_argument("--no-cache", action="store_true", help="Disable JSONL translation cache.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing *_zhcn.tex files.")
    parser.add_argument("--dry-run", action="store_true", help="List files that would be translated without API calls.")
    parser.add_argument("--min-paragraphs", type=int, default=1, help="Minimum detected prose paragraphs for a content file.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    files = find_tex_files(args.paths)
    if not files:
        raise SystemExit("No .tex files found.")

    config = None if args.dry_run else get_config(args)
    translatable_sources = collect_translatable_sources(files, args.min_paragraphs)
    counts = {"translated": 0, "updated": 0, "would_translate": 0, "skip": 0, "exists": 0}

    for tex_path in files:
        status, paragraph_count, out_path = process_file(
            tex_path,
            config=config,
            args=args,
            translatable_sources=translatable_sources,
        )
        counts[status] += 1
        if status == "skip":
            print(f"SKIP  {tex_path} ({paragraph_count} prose paragraphs)")
        elif status == "updated":
            print(f"UPDAT {out_path} ({paragraph_count} prose paragraphs)")
        elif status == "exists":
            print(f"EXIST {out_path} ({paragraph_count} prose paragraphs)")
        elif status == "would_translate":
            print(f"TODO  {tex_path} -> {out_path} ({paragraph_count} prose paragraphs)")
        else:
            print(f"OK    {tex_path} -> {out_path} ({paragraph_count} prose paragraphs)")

    print(
        "Summary: "
        + ", ".join(f"{name}={value}" for name, value in counts.items() if value),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
