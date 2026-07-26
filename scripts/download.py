#!/usr/bin/env python3
"""Download and checksum-verify Binance archives from a manifest."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from paths import DATA_DIR, RAW_DIR, ensure_directories  # noqa: E402

BASE = "https://data.binance.vision/"


def remote_bytes(key: str, retries: int = 5) -> bytes:
    url = BASE + urllib.parse.quote(key)
    error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except Exception as exc:
            error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to download {key}: {error}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def destination(key: str) -> Path:
    return RAW_DIR / Path(key).relative_to("data/spot/monthly/klines")


def fetch(key: str) -> tuple[str, str | None]:
    try:
        checksum = remote_bytes(key + ".CHECKSUM").decode().strip().split()[0]
        if len(checksum) != 64:
            raise ValueError(f"invalid checksum response for {key}")
        target = destination(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size and sha256(target) == checksum:
            return key, None
        payload = remote_bytes(key)
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(payload)
        actual = sha256(temporary)
        if actual != checksum:
            raise ValueError(
                f"checksum mismatch for {key}: expected {checksum}, got {actual}"
            )
        os.replace(temporary, target)
        return key, None
    except Exception as exc:
        return key, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        default=DATA_DIR / "manifest.txt",
    )
    parser.add_argument("--workers", type=int, default=48)
    args = parser.parse_args()
    ensure_directories()

    keys = [
        line.strip()
        for line in args.manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not keys:
        raise SystemExit(f"empty manifest: {args.manifest}")

    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for completed, (key, error) in enumerate(executor.map(fetch, keys), 1):
            if error:
                failures.append((key, error))
            if completed % 1000 == 0 or completed == len(keys):
                print(
                    f"{completed}/{len(keys)} processed; "
                    f"{len(failures)} failed",
                    flush=True,
                )

    failure_path = DATA_DIR / "download_failures.tsv"
    failure_path.write_text(
        "".join(f"{key}\t{error}\n" for key, error in failures),
        encoding="utf-8",
    )
    if failures:
        raise SystemExit(f"{len(failures)} downloads failed; see {failure_path}")


if __name__ == "__main__":
    main()
