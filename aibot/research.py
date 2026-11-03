"""Opt-in research tools: Brave web search, public pages/PDFs, YouTube transcripts.

Fetches go through WEB_PROXY_URL when set, refuse non-public hosts and addresses,
follow at most a few re-checked redirects, and are bounded in size and time.
The pre-connection address check cannot stop DNS rebinding or a misbehaving
proxy; the deployment's egress policy remains the enforcing control.
Tool output is untrusted material for the model, never instructions.
"""

import asyncio
import io
import ipaddress
import json
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
import requests
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from yarl import URL
from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig

log = logging.getLogger(__name__)

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
USER_AGENT = "AtlasBot/1.0 (Discord community bot)"
FETCH_TIMEOUT = 20
MAX_DOWNLOAD = 10 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_RESULT = 12000
PDF_PAGES = 30
INTERNAL_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home.arpa", ".svc")
YOUTUBE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)

READ_URL = {
    "type": "function",
    "function": {
        "name": "read_url",
        "description": (
            "Read a public web page, PDF or YouTube video. Returns the page text, PDF text "
            "with [page N] markers, or the video's transcript with [m:ss] timestamps."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "A public http(s) URL"}},
            "required": ["url"],
        },
    },
}
WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web. Returns titles, URLs and snippets; read_url the best ones.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
        },
    },
}


class Blocked(ValueError):
    """The URL is not a public http(s) address."""


async def check_public(url: str):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise Blocked("only http(s) URLs are allowed")
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise Blocked("URLs with credentials or unusual ports are not allowed")
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(INTERNAL_SUFFIXES):
        raise Blocked("internal host names are not allowed")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        if "." not in host:
            raise Blocked("internal host names are not allowed") from None
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parts.port or 443, type=socket.SOCK_STREAM
        )
        addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
    if not addresses or not all(address.is_global for address in addresses):
        raise Blocked("the address is not public")


def youtube_id(url: str) -> str | None:
    match = YOUTUBE.search(url)
    return match.group(1) if match else None


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "nav", "header", "footer", "aside"}
    SKIP_ROLES = {"navigation", "menu", "menubar", "search", "banner"}
    MAIN = {"main", "article"}
    BREAKS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.parts, self.main, self.title = [], [], ""
        # Skipping tracks only the tag that started it, so unclosed <p>/<li> can't stick.
        self.skip_tag, self.skip, self.in_main = None, 0, 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            self.skip += tag == self.skip_tag
            return
        if tag in self.SKIP or dict(attrs).get("role") in self.SKIP_ROLES:
            self.skip_tag, self.skip = tag, 1
            return
        self.in_main += tag in self.MAIN
        self.in_title = tag == "title"
        if tag in self.BREAKS:
            self._add("\n")

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip -= 1
                if not self.skip:
                    self.skip_tag = None
            return
        if tag in self.MAIN and self.in_main:
            self.in_main -= 1
        if tag in self.BREAKS:
            self._add("\n")
        self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip_tag:
            self._add(data)

    def _add(self, text):
        self.parts.append(text)
        if self.in_main:
            self.main.append(text)


def html_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    # Prefer <main>/<article> content so menus don't use up the result budget.
    text = "".join(parser.main if "".join(parser.main).strip() else parser.parts)
    lines = (" ".join(line.split()) for line in text.splitlines())
    body = "\n".join(line for line in lines if line)
    title = " ".join(parser.title.split())
    return f"Title: {title}\n\n{body}" if title else body


def pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    pages = [
        f"[page {number}] {' '.join((page.extract_text() or '').split())}"
        for number, page in enumerate(reader.pages[:PDF_PAGES], 1)
    ]
    note = (
        f"\n(First {PDF_PAGES} of {len(reader.pages)} pages.)"
        if len(reader.pages) > PDF_PAGES
        else ""
    )
    return "\n".join(pages) + note


class _Timeout(HTTPAdapter):
    def send(self, request, **kwargs):
        kwargs["timeout"] = kwargs.get("timeout") or FETCH_TIMEOUT
        return super().send(request, **kwargs)


class Research:
    def __init__(self, brave_key: str = "", proxy: str | None = None):
        self.brave_key = brave_key
        self.proxy = proxy
        self.session: aiohttp.ClientSession | None = None

    def tools(self) -> list[dict]:
        return [WEB_SEARCH, READ_URL] if self.brave_key else [READ_URL]

    async def call(self, name: str, arguments: str | None) -> str:
        """Run one model-requested tool; failures become text, never exceptions."""
        try:
            args = json.loads(arguments or "{}")
            if name == "web_search" and self.brave_key:
                result = await self.search(str(args["query"]), int(args.get("count", 5)))
            elif name == "read_url":
                result = await self.read_url(str(args["url"]))
            else:
                return f"Unknown tool {name!r}; nothing was run."
        except Exception as exc:
            log.info("Tool %s failed: %s", name, type(exc).__name__)
            return f"Tool failed ({type(exc).__name__}): {str(exc)[:300]}"
        return result[:MAX_RESULT]

    async def close(self):
        if self.session:
            await self.session.close()

    async def _get(self, url: str, headers=None) -> tuple[str, bytes, str]:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
            )
        for _ in range(MAX_REDIRECTS + 1):
            await check_public(url)
            async with self.session.get(
                url, headers=headers, proxy=self.proxy, allow_redirects=False
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    target = urljoin(url, response.headers.get("Location", ""))
                    source_url, target_url = URL(url), URL(target)
                    if source_url.origin() != target_url.origin():
                        # API credentials belong only to the origin they were sent to.
                        headers = None
                    url = target
                    continue
                response.raise_for_status()
                if (response.content_length or 0) > MAX_DOWNLOAD:
                    raise ValueError("the response is larger than 10 MB")
                body = bytearray()
                async for piece in response.content.iter_chunked(65536):
                    body += piece
                    if len(body) > MAX_DOWNLOAD:
                        raise ValueError("the response is larger than 10 MB")
                return response.headers.get("Content-Type", ""), bytes(body), url
        raise ValueError("too many redirects")

    async def search(self, query: str, count: int = 5) -> str:
        url = str(URL(BRAVE_URL).with_query(q=query, count=max(1, min(count, 8))))
        _, body, _ = await self._get(
            url, headers={"Accept": "application/json", "X-Subscription-Token": self.brave_key}
        )
        results = json.loads(body).get("web", {}).get("results", [])
        if not results:
            return f"No results for {query!r}."
        return "\n\n".join(
            f"{item.get('title', '')}\n{item.get('url', '')}\n"
            + re.sub(r"<[^>]+>", "", item.get("description", ""))
            for item in results
        )

    async def read_url(self, url: str) -> str:
        if youtube_id(url):
            return await self.youtube(url)
        kind, body, final = await self._get(url)
        kind = kind.split(";")[0].strip().lower()
        if kind == "application/pdf" or body.startswith(b"%PDF-"):
            text = await asyncio.to_thread(pdf_text, body)
        elif kind in {"text/html", "application/xhtml+xml"}:
            text = html_text(body.decode("utf-8", errors="replace"))
        elif kind.startswith("text/") or kind in {"application/json", "application/xml"}:
            text = body.decode("utf-8", errors="replace")
        else:
            return f"Cannot read content type {kind or 'unknown'} at {final}."
        return f"Source: {final}\n\n{text}"

    async def youtube(self, url: str) -> str:
        video = youtube_id(url)
        canonical = f"https://www.youtube.com/watch?v={video}"
        header = f"Source: {canonical}"
        try:
            oembed = URL("https://www.youtube.com/oembed").with_query(url=canonical, format="json")
            meta = json.loads((await self._get(str(oembed)))[1])
            header += f"\nTitle: {meta.get('title')}\nChannel: {meta.get('author_name')}"
        except Exception as exc:
            log.info("YouTube metadata failed: %s", type(exc).__name__)
        lines = await asyncio.to_thread(self._transcript, video)
        return f"{header}\n\nTranscript:\n" + "\n".join(lines)

    def _transcript(self, video: str) -> list[str]:
        session = requests.Session()
        session.mount("https://", _Timeout())
        proxy = GenericProxyConfig(self.proxy, self.proxy) if self.proxy else None
        api = YouTubeTranscriptApi(proxy_config=proxy, http_client=session)
        try:
            transcript = api.fetch(video, languages=["en", "en-US", "en-GB"])
        except NoTranscriptFound:
            transcript = next(iter(api.list(video))).fetch()
        return [
            f"[{int(part.start) // 60}:{int(part.start) % 60:02d}] {part.text}"
            for part in transcript
        ]
