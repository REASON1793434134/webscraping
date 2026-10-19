"""Download the HTML source of Douban Movie Top 250 pages.

The program keeps the raw response bytes, uses a modest delay between
requests, and does not attempt to bypass CAPTCHA, login, or other access
controls.yes

Examples:
    python webscraping.py
    python webscraping.py --output data --delay 3 --fetch-details
    python webscraping.py --start-page 1 --end-page 2 --overwrite
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://movie.douban.com/top250"
PAGE_SIZE = 25
PAGE_COUNT = 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
LOGGER = logging.getLogger("douban-top250")


class SubjectLinkParser(HTMLParser):
    """Extract movie detail links from a Top 250 listing page."""

    def __init__(self) -> None:
        super().__init__()
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        parsed = urlparse(href)
        if parsed.netloc not in {"movie.douban.com", ""}:
            return
        if not re.fullmatch(r"/subject/\d+/?", parsed.path):
            return
        self.links.add(f"https://movie.douban.com{parsed.path.rstrip('/')}")


@dataclass(frozen=True)
class FetchRecord:
    """One downloaded resource, suitable for writing to ``manifest.json``."""

    kind: str
    url: str
    file: str
    bytes_written: int
    fetched_at: str


class DoubanTop250Scraper:
    """Fetch Top 250 listing pages and, optionally, movie detail pages."""

    def __init__(
        self,
        output_dir: Path,
        delay: float = 2.0,
        timeout: float = 20.0,
        retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        overwrite: bool = False,
    ) -> None:
        if delay < 0 or timeout <= 0 or retries < 0:
            raise ValueError("delay must be >= 0; timeout must be > 0; retries must be >= 0")
        self.output_dir = output_dir
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.overwrite = overwrite
        self.records: list[FetchRecord] = []

    @staticmethod
    def page_url(page: int) -> str:
        """Return a standard Top 250 URL for a one-based page number."""
        if not 1 <= page <= PAGE_COUNT:
            raise ValueError(f"page must be between 1 and {PAGE_COUNT}")
        return f"{BASE_URL}?{urlencode({'start': (page - 1) * PAGE_SIZE, 'filter': ''})}"

    def fetch(self, url: str) -> bytes:
        """Fetch one URL with bounded retries and exponential backoff."""
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                "User-Agent": self.user_agent,
            },
        )
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except HTTPError as exc:
                LOGGER.warning("HTTP %s for %s (attempt %d)", exc.code, url, attempt + 1)
                if exc.code not in {408, 429, 500, 502, 503, 504}:
                    raise
            except URLError as exc:
                LOGGER.warning("Network error for %s: %s (attempt %d)", url, exc.reason, attempt + 1)
            if attempt < self.retries:
                time.sleep(max(self.delay, 1.0) * (2**attempt))
        raise RuntimeError(f"failed to fetch after {self.retries + 1} attempts: {url}")

    def save(self, kind: str, url: str, relative_path: Path) -> bytes | None:
        """Download and save one response, returning its bytes on success."""
        destination = self.output_dir / relative_path
        if destination.exists() and not self.overwrite:
            LOGGER.info("skip existing %s", destination)
            return destination.read_bytes()
        try:
            content = self.fetch(url)
        except (HTTPError, RuntimeError, URLError) as exc:
            LOGGER.error("could not download %s: %s", url, exc)
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        self.records.append(
            FetchRecord(
                kind=kind,
                url=url,
                file=relative_path.as_posix(),
                bytes_written=len(content),
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        LOGGER.info("saved %s (%d bytes)", destination, len(content))
        return content

    def listing_pages(self, start_page: int, end_page: int) -> Iterable[tuple[int, bytes]]:
        for page in range(start_page, end_page + 1):
            if page > start_page:
                time.sleep(self.delay)
            url = self.page_url(page)
            content = self.save("listing", url, Path("pages") / f"page_{page:02d}.html")
            if content is not None:
                yield page, content

    def detail_links(self, pages: Iterable[tuple[int, bytes]]) -> list[str]:
        links: set[str] = set()
        for _, content in pages:
            parser = SubjectLinkParser()
            parser.feed(content.decode("utf-8", errors="ignore"))
            links.update(parser.links)
        return sorted(links, key=self.subject_id)

    @staticmethod
    def subject_id(url: str) -> int:
        match = re.search(r"/subject/(\d+)", url)
        return int(match.group(1)) if match else 0

    def download_details(self, links: Iterable[str]) -> None:
        for index, url in enumerate(links):
            if index:
                time.sleep(self.delay)
            subject_id = self.subject_id(url)
            suffix = str(subject_id) if subject_id else hashlib.sha1(url.encode()).hexdigest()[:12]
            self.save("detail", url, Path("details") / f"subject_{suffix}.html")

    def write_manifest(self) -> None:
        manifest = self.output_dir / "manifest.json"
        manifest.write_text(
            json.dumps([record.__dict__ for record in self.records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def run(self, start_page: int, end_page: int, fetch_details: bool = False) -> int:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        pages = list(self.listing_pages(start_page, end_page))
        if fetch_details:
            self.download_details(self.detail_links(pages))
        self.write_manifest()
        return len(self.records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("douban_top250_html"))
    parser.add_argument("--start-page", type=int, default=1, choices=range(1, PAGE_COUNT + 1))
    parser.add_argument("--end-page", type=int, default=PAGE_COUNT, choices=range(1, PAGE_COUNT + 1))
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between requests (default: 2)")
    parser.add_argument("--timeout", type=float, default=20.0, help="request timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="retries after a transient error")
    parser.add_argument("--overwrite", action="store_true", help="redownload existing files")
    parser.add_argument("--fetch-details", action="store_true", help="also download each movie detail page")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    if args.start_page > args.end_page:
        raise SystemExit("--start-page must be less than or equal to --end-page")
    scraper = DoubanTop250Scraper(
        output_dir=args.output,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        user_agent=args.user_agent,
        overwrite=args.overwrite,
    )
    saved = scraper.run(args.start_page, args.end_page, args.fetch_details)
    LOGGER.info("finished: %d new file(s), output=%s", saved, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
