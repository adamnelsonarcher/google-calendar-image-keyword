#!/usr/bin/env python3
"""
Check whether a Google Calendar flair image URL is still reachable (HTTP 200).

Uses the URL pattern from the repository README:
  https://ssl.gstatic.com/tmly/f8944938hffheth4ew890ht4i8/flairs/xxhdpi/img_[ID].jpg

Pass a full URL, an image id (e.g. coffee -> img_coffee.jpg), or a keyword phrase;
phrases try a few common slug shapes because Google does not publish a single rule.

Batch: --keywords-file en_us/keywords.md checks every keyword line (TSV to stdout).
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = (
    "https://ssl.gstatic.com/tmly/f8944938hffheth4ew890ht4i8/flairs/xxhdpi/img_{id}.jpg"
)


def _slug_variants(phrase: str) -> list[str]:
    s = phrase.strip().lower()
    out: list[str] = []

    def add(x: str) -> None:
        x = x.strip()
        if x and x not in out:
            out.append(x)

    if re.fullmatch(r"[a-z0-9_]+", s):
        add(s)
    add(re.sub(r"\s+", "", s))
    add(re.sub(r"\s+", "_", s))
    add(re.sub(r"[^a-z0-9]+", "_", s).strip("_"))
    if "2" in s:
        d = s.replace("2", "to")
        if d != s:
            if re.fullmatch(r"[a-z0-9_]+", d):
                add(d)
            add(re.sub(r"\s+", "", d))
            add(re.sub(r"\s+", "_", d))
            add(re.sub(r"[^a-z0-9]+", "_", d).strip("_"))
    return out


def _url_from_id(image_id: str) -> str:
    image_id = image_id.strip()
    if image_id.startswith("http://") or image_id.startswith("https://"):
        return image_id
    m = re.fullmatch(r"img_([a-z0-9_]+)\.jpg", image_id, flags=re.IGNORECASE)
    if m:
        image_id = m.group(1)
    return BASE.format(id=image_id)


def _request_head(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, url
    except urllib.error.HTTPError as e:
        if e.code in (405, 501):
            return _request_get(url, timeout)
        return e.code, url
    except urllib.error.URLError:
        return _request_get(url, timeout)


def _request_get(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, url
    except urllib.error.HTTPError as e:
        return e.code, url


def check_url(url: str, timeout: float) -> tuple[int, str]:
    return _request_head(url, timeout)


def _urls_for_target(raw: str) -> list[str]:
    if raw.startswith("http://") or raw.startswith("https://"):
        return [raw]
    if "/" in raw or raw.endswith(".jpg"):
        raise ValueError(
            "pass a full https URL, or an id/phrase without path slashes."
        )
    variants = _slug_variants(raw)
    seen: set[str] = set()
    urls: list[str] = []
    for v in variants:
        u = _url_from_id(v)
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _check_all(urls: list[str], timeout: float) -> list[tuple[int, str]]:
    return [check_url(url, timeout) for url in urls]


def iter_keywords_md(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("See [") and "README" in line:
            continue
        out.append(line)
    return out


def check_phrase_first_hit(phrase: str, timeout: float) -> tuple[int, str]:
    """Return (status, url) for first candidate that returns 200, else last attempt."""
    urls = _urls_for_target(phrase)
    if not urls:
        return 404, ""
    last: tuple[int, str] = (0, "")
    for u in urls:
        code, final = check_url(u, timeout)
        last = (code, final)
        if code == 200:
            return code, final
    return last


def _run_batch(
    path: Path,
    timeout: float,
    jobs: int,
    no_header: bool,
) -> int:
    keywords = iter_keywords_md(path)
    if not keywords:
        print("error: no keywords found in file", file=sys.stderr)
        return 2

    if not no_header:
        print("keyword\thttp_status\turl", flush=True)

    rows: list[tuple[str, int, str] | None] = [None] * len(keywords)

    def work(i: int, phrase: str) -> tuple[int, str, int, str]:
        code, url = check_phrase_first_hit(phrase, timeout)
        return i, phrase, code, url

    if jobs <= 1:
        for i, phrase in enumerate(keywords):
            code, url = check_phrase_first_hit(phrase, timeout)
            rows[i] = (phrase, code, url)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = [ex.submit(work, i, phrase) for i, phrase in enumerate(keywords)]
            for fut in as_completed(futures):
                i, phrase, code, url = fut.result()
                rows[i] = (phrase, code, url)

    ok = 0
    for row in rows:
        assert row is not None
        phrase, code, url = row
        print(f"{phrase}\t{code}\t{url}", flush=True)
        if code == 200:
            ok += 1
    print(f"# summary: {ok}/{len(keywords)} returned HTTP 200", file=sys.stderr)
    return 0 if ok == len(keywords) else 1


def _emit_results(results: list[tuple[int, str]], show_all: bool) -> int:
    if show_all or len(results) == 1:
        for code, final in results:
            print(f"{code}\t{final}")
        return 0 if any(c == 200 for c, _ in results) else 1
    for code, final in results:
        if code == 200:
            print(f"{code}\t{final}")
            return 0
    print(
        "No candidate returned HTTP 200. Re-run with --all to list each URL tried.",
        file=sys.stderr,
    )
    for code, final in results:
        print(f"{code}\t{final}", file=sys.stderr)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Check flair image URL reachability (see README URL template)."
    )
    p.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Full https URL, image id (e.g. coffee), or a phrase to derive ids from",
    )
    p.add_argument(
        "--keywords-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Markdown file of keywords (one per non-empty block), e.g. en_us/keywords.md",
    )
    p.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="Parallel workers when using --keywords-file (default: 1)",
    )
    p.add_argument(
        "--no-header",
        action="store_true",
        help="With --keywords-file, omit the TSV header row",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds per request (default: 15)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="When deriving from a phrase, print every candidate URL and status",
    )
    args = p.parse_args()

    if bool(args.keywords_file) == bool(args.target):
        p.error("Provide exactly one of: TARGET (positional) or --keywords-file")

    if args.keywords_file is not None:
        path = args.keywords_file
        if not path.is_file():
            print(f"error: not a file: {path}", file=sys.stderr)
            return 2
        if args.all:
            p.error("--all applies only to single TARGET mode")
        if args.jobs < 1:
            p.error("--jobs must be >= 1")
        return _run_batch(path, args.timeout, args.jobs, args.no_header)

    raw = args.target.strip()
    try:
        urls = _urls_for_target(raw)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    results = _check_all(urls, args.timeout)
    return _emit_results(results, args.all)


if __name__ == "__main__":
    raise SystemExit(main())
