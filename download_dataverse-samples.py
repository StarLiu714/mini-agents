#!/usr/bin/env python3
"""Download all files listed by a DataVerse dataset share page."""

import argparse
import filecmp
import hashlib
import http.client
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen


SITE = "https://dataset.dataverse.zone"
PREVIEW_API = f"{SITE}/api/data4o/dataset/get-preview"
FILE_ORIGIN = "https://d1bp2lmc7eppr7.cloudfront.net"
SHARE_PATH = re.compile(r"/share/dataset/([\w-]+)/datasets/([\w-]+)/?")


@dataclass(frozen=True)
class SampleFile:
    path: PurePosixPath
    url: str
    size: int


class IntegrityError(ValueError):
    """A file differs from the size or checksum provided by DataVerse."""


def with_retry(operation, *args):
    for attempt in range(4):
        try:
            return operation(*args)
        except HTTPError as error:
            error.close()
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 3:
                raise
        except (URLError, TimeoutError, ConnectionError, http.client.IncompleteRead, IntegrityError):
            if attempt == 3:
                raise
        time.sleep(2**attempt)


def get_manifest(share_url):
    url = urlsplit(share_url)
    match = SHARE_PATH.fullmatch(url.path)
    if f"{url.scheme}://{url.netloc}" != SITE or not match:
        raise ValueError("Expected a DataVerse /share/dataset/<shareId>/datasets/<datasetId> URL")
    share_id, dataset_id = match.groups()
    env = parse_qs(url.query).get("env", ["en"])[0]
    # The share page sends these three fields to get-preview. Its version
    # query parameter is not included in the preview API request.
    request = Request(
        PREVIEW_API,
        data=json.dumps({"shareId": share_id, "datasetId": dataset_id, "env": env}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("code") != 200:
        raise ValueError(f"DataVerse preview API returned code {result.get('code')}")

    data = result["data"]
    files = []
    seen = set()
    for item in data["sampleInfos"]:
        meta = item["meta"]
        path = PurePosixPath(meta["relativePath"])
        if path.is_absolute() or len(path.parts) < 2 or ".." in path.parts or "\\" in str(path):
            raise ValueError(f"Invalid sample path: {path}")
        path_key = unicodedata.normalize("NFC", str(path)).casefold()
        if path_key in seen:
            raise ValueError(f"Duplicate sample path: {path}")
        seen.add(path_key)

        if item.get("appendixLink"):
            raise ValueError(f"The manifest contains an unsupported appendix entry: {path}")
        link = item["link"]
        if not link.startswith("/datasets/dataset-samples/"):
            raise ValueError(f"Unexpected download link for {path}")
        size = meta["size"]
        if type(size) is not int or size < 0:
            raise ValueError(f"Invalid file size for {path}")
        files.append(SampleFile(path, FILE_ORIGIN + quote(link, safe="/%"), size))

    return data["name"], files


def download_file(root, sample):
    target = root.joinpath(*sample.path.parts)
    if target.resolve() != target:
        raise ValueError("Destination path contains a symbolic link")
    target.parent.mkdir(parents=True, exist_ok=True)

    request = Request(sample.url, headers={"Accept-Encoding": "identity"})
    with urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise IntegrityError(f"Unexpected download status {response.status}")
        etag = response.headers.get("ETag", "").strip('"').lower()
        # These CDN objects use an MD5 ETag when the tag is a plain hex digest.
        digest = hashlib.md5() if re.fullmatch(r"[0-9a-f]{32}", etag) else None
        if digest and target.is_file() and target.stat().st_size == sample.size:
            with target.open("rb") as existing:
                for chunk in iter(lambda: existing.read(256 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != etag:
                raise FileExistsError("Existing file differs from the remote file")
            return True

        with TemporaryDirectory(prefix=".dataverse-", dir=target.parent) as staging:
            partial = Path(staging) / "download"
            with partial.open("wb") as output:
                for chunk in iter(lambda: response.read(256 * 1024), b""):
                    output.write(chunk)
                    if digest:
                        digest.update(chunk)
                size = output.tell()
            if size != sample.size:
                raise IntegrityError(f"Expected {sample.size} bytes, received {size}")
            if digest and digest.hexdigest() != etag:
                raise IntegrityError("Checksum differs from the CDN ETag")

            # Publish the complete file atomically without overwriting an existing file.
            try:
                os.link(partial, target)
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or not filecmp.cmp(target, partial, shallow=False):
                    raise FileExistsError("Existing file differs from the remote file") from None
    return digest is not None


def download_dataset(share_url, destination, workers):
    name, files = with_retry(get_manifest, share_url)
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    samples = Counter(file.path.parts[0] for file in files)
    print(f"{name}: {len(samples)} samples, {len(files)} files → {root}", flush=True)
    for sample, count in samples.items():
        print(f"  {sample}: {count} files", flush=True)

    failures = 0
    checksums = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(with_retry, download_file, root, file): file for file in files}
        for count, future in enumerate(as_completed(pending), 1):
            file = pending[future]
            try:
                checksums += future.result()
                target = root.joinpath(*file.path.parts)
                if target.stat().st_size != file.size:
                    raise IntegrityError("Local file size differs from the manifest")
            except (OSError, ValueError, http.client.HTTPException) as error:
                failures += 1
                print(f"  Failed: {file.path}: {error}", file=sys.stderr, flush=True)
            if count % 25 == 0 or count == len(files):
                print(f"  {count}/{len(files)} processed, {failures} failed", flush=True)

    print(f"{name}: {len(files) - failures}/{len(files)} verified, {checksums} checksum matches", flush=True)
    return failures == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", nargs=2, action="append", required=True, metavar=("SHARE_URL", "DIRECTORY"),
        help="Share page URL and output directory. Repeat for multiple datasets.",
    )
    parser.add_argument("--workers", type=int, default=12, help="Concurrent downloads (default: 12).")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    succeeded = True
    for share_url, directory in args.dataset:
        try:
            if not download_dataset(share_url, Path(directory).expanduser(), args.workers):
                succeeded = False
        except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException) as error:
            print(f"{directory}: {error}", file=sys.stderr)
            succeeded = False
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
