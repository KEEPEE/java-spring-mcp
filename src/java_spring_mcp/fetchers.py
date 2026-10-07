"""Fetchers for JDK API docs, Spring Boot reference and Maven Central.

Public entry points (stable signatures - later phases depend on them):

- ``fetch_jdk_class(fully_qualified, module="java.base") -> dict``
- ``fetch_spring_boot_section(section="index") -> dict``
- ``fetch_maven_artifact(group_id, artifact_id, version=None) -> dict``

Each returns ``{"ok": True, ...}`` on success and ``{"ok": False, "error": ...}``
on failure; none of them raise.  The HTML/JSON parsing lives in pure functions
(``parse_jdk_html``, ``parse_spring_section_html``, ``parse_maven_search_json``)
so it can be tested offline against fixtures in ``tests/fixtures/``.

No network access happens at import time.

Politeness: every HTTP request goes through :mod:`java_spring_mcp.politeness`
— robots.txt rules, per-host throttle (``docs.spring.io`` publishes
``Crawl-delay: 1``), ``429``/``503``/``504`` handling with ``Retry-After``,
stall detection, conditional GET (ETag / Last-Modified) and a per-tool-call
request budget. The layer never raises and never changes the return shape; a
robots-disallowed URL comes back as
``{"ok": False, "error": "… blocked by robots.txt …"}``. Opt out with
``JAVA_SPRING_MCP_POLITENESS_DISABLED=1`` (at the user's own risk).

There used to be a second transport here — a ``curl`` subprocess fallback with
``--max-time 90`` / ``subprocess timeout=105`` — justified in its own comment by
"edges that throttle Python's TLS fingerprint". That premise is not supported
by the audit (A2 §4.2: ``search.maven.org`` answered the audit UA in 0.37 s and
the *browser* UA this module used in 30.6 s — the stall is intermittent and
server-side, not TLS-driven), and the fallback bypassed robots, throttle,
budget and validators entirely. It has been removed; the politeness layer's
transport retry + backoff + 10 s stall detection covers the real failure mode
in ≤ ~20 s instead of ~2 min.
"""

from __future__ import annotations

import json as _json
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as _markdownify

from .cache import DocCache
from .politeness import Politeness, default_robots_db_path

JDK_API_BASE = "https://docs.oracle.com/en/java/javase/26/docs/api"
SPRING_REFERENCE_BASE = "https://docs.spring.io/spring-boot/reference"
MAVEN_SEARCH_URL = "https://search.maven.org/solrsearch/select"

#: Honest, contactable UA (A1 §6 / A2 §4.3).  This module used to send a
#: Chrome 120 spoof; the audit showed it bought nothing (it made
#: ``search.maven.org`` *slower*, 30.6 s vs 0.37 s) and it defeats
#: ``User-agent:``-specific robots rules, because a robots file can only match
#: a token it can see.
USER_AGENT = (
    "java-spring-mcp/0.1 (+https://github.com/KEEPEE/java-spring-mcp)"
)

#: Kept only as a compatibility alias (``server`` used to import it for its
#: probes).  The politeness layer sets the UA on every request itself; nothing
#: here spoofs a browser any more.
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

#: connect 5 s / read 10 s / total 15 s — the values A2 §4.3 settled on.  The
#: old 20 s total plus a 90 s curl fallback let one stalled edge hold a tool
#: call for ~2 minutes.
REQUEST_TIMEOUT = 15.0
TIMEOUTS = (5.0, 10.0, REQUEST_TIMEOUT)

#: The only hosts this module ever contacts.  Handed to the politeness layer as
#: an allowlist so an unexpected redirect or a malformed identifier can never
#: turn a docs lookup into a request to somebody else's site.
ALLOWED_HOSTS = frozenset({"docs.oracle.com", "docs.spring.io", "search.maven.org"})

#: Hard cap on **requests on the wire** for one MCP tool call (A2 §4.3
#: ``MAX_REQUESTS_PER_TOOL_CALL``).  Since A8 F3 one budget unit *is* one
#: request: the robots.txt fetch, the first try, every ``429``/transport retry
#: and every redirect hop all pay.  Measured live in the A9 smoke:
#:
#: * cold legit call  = 2 units (robots.txt + the page) — ``java_docs`` with a
#:   cold index build = 3 (robots + allclasses + class page),
#: * cold error call  = the cap itself: the module-guessing loop stops at the
#:   first denial, so one typo costs at most this many requests.
#:
#: 4 was too tight after F3: a legit cold call already needs 3, and Oracle
#: answers a wrong module with ``302 → /en/java/javase/26/``, i.e. **2 units per
#: guess** (the 302 and the hop).  6 leaves a legit call three units of reserve
#: (one full redirect chain plus a retry) while the wire ceiling stays at 6 —
#: below the 8 requests the *old* limit 4 actually put on the wire (A5 §5).
FETCH_BUDGET_LIMIT = 6

#: TTL for the raw body + validators the fetcher keeps for conditional GET.
#: Matches the server's docs TTL so a revalidation window always exists.
REVALIDATION_TTL_SECONDS = 7 * 24 * 3600

#: Stable prefix of the layer's budget message (``Politeness._get``); callers
#: use it to stop a module-guessing loop instead of burning the remaining
#: candidates on requests that will never be made.
_BUDGET_ERROR_PREFIX = "request budget exhausted"

__all__ = [
    "fetch_jdk_class",
    "fetch_spring_boot_section",
    "fetch_maven_artifact",
    "parse_jdk_html",
    "parse_spring_section_html",
    "parse_maven_search_json",
    "get_politeness",
    "set_politeness",
    "tool_budget",
    "current_budget",
]


# ---------------------------------------------------------------------------
# Politeness layer (process-wide singleton)
# ---------------------------------------------------------------------------

_politeness: Politeness | None = None
_politeness_lock = threading.Lock()


def get_politeness() -> Politeness:
    """Process-wide politeness layer (created on first use).

    The robots cache is a SQLite file next to ``cache.db`` so it survives
    restarts (1 robots request per host per 7 days, not per call). If that
    directory is not writable the layer falls back to a per-process in-memory
    cache — politeness still applies, it just forgets across restarts. A
    missing cache must never take a tool down. Tests replace the instance
    through :func:`set_politeness`.
    """
    global _politeness
    if _politeness is None:
        with _politeness_lock:
            if _politeness is None:
                kwargs: dict = dict(timeouts=TIMEOUTS, allowed_hosts=ALLOWED_HOSTS)
                try:
                    _politeness = Politeness(
                        USER_AGENT, cache_path=default_robots_db_path(), **kwargs
                    )
                except Exception:
                    _politeness = Politeness(USER_AGENT, cache_path=None, **kwargs)
    return _politeness


def set_politeness(politeness: Politeness | None) -> None:
    """Replace (or with ``None`` reset) the process-wide layer. Test seam."""
    global _politeness
    with _politeness_lock:
        _politeness = politeness


#: Budget bound by the enclosing :func:`tool_budget` (``(scope, limit)``).
#: A ContextVar, not a module global, so a tool call running in a worker
#: thread can never spend another call's budget.
_current_budget: ContextVar[tuple[str, int] | None] = ContextVar(
    "java_spring_mcp_request_budget", default=None
)


@contextmanager
def tool_budget(name: str, limit: int = FETCH_BUDGET_LIMIT):
    """Bind one request budget to everything fetched inside this block.

    ``java_docs`` resolves a fully-qualified name by trying ``java.base`` and
    then five fallback modules, and Oracle answers a wrong name with HTTP 200
    (a soft-200 home page, always the same 33 756 B), so nothing in the status
    line stops that loop. The budget does: one tool call, at most ``limit``
    requests, index build included. The scope name is stable per tool and
    reset on entry, so ``Politeness._budgets`` cannot grow without bound.
    """
    scope = f"tool:{name}"
    try:
        get_politeness().reset_budget(scope)
    except Exception:
        pass  # a broken budget layer must not stop a tool
    token = _current_budget.set((scope, int(limit)))
    try:
        yield scope
    finally:
        _current_budget.reset(token)


def current_budget() -> tuple[str, int] | None:
    """The budget bound by the enclosing :func:`tool_budget`, if any."""
    return _current_budget.get()


def _doc_cache() -> DocCache | None:
    """DocCache for revalidation data, or ``None`` if it cannot be opened.

    A cache problem must never turn into a fetch failure.
    """
    try:
        return DocCache()
    except Exception:
        return None


def _revalidation_for(url: str) -> tuple[dict | None, bytes | None]:
    """``(validators, cached_body)`` stored for ``url`` by an earlier fetch.

    Both are returned together or not at all: a conditional GET without a body
    to serve would throw away the ``304`` (A3 §5.4).
    """
    cache = _doc_cache()
    if cache is None:
        return None, None
    try:
        entry = cache.get_entry(url, include_expired=True)
    except Exception:
        return None, None
    if not entry or not entry.get("body"):
        return None, None
    validators = {
        k: v
        for k, v in (("etag", entry.get("etag")), ("last_modified", entry.get("last_modified")))
        if v
    }
    if not validators:
        return None, None
    return validators, str(entry["body"]).encode("utf-8", "replace")


def _remember_revalidation(url: str, validators: dict, body: str) -> None:
    """Persist validators + raw body so the next fetch can send a conditional GET.

    ``search.maven.org`` sends no ``ETag`` and no ``Last-Modified`` (A2 §3), so
    for that host this is a no-op by design — there is nothing to revalidate
    with, and the layer counts the conditional GET as skipped.
    """
    if not validators:
        return
    cache = _doc_cache()
    if cache is None:
        return
    try:
        cache.set_validators(
            url,
            etag=validators.get("etag"),
            last_modified=validators.get("last_modified"),
            body=body,
            ttl_seconds=REVALIDATION_TTL_SECONDS,
        )
    except Exception:
        pass  # never break a successful fetch over cache bookkeeping


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class _Response:
    """Minimal response shim shared by every fetch path in this module.

    Exactly one of ``text`` / ``error`` is meaningful.  ``status_code`` is
    *truthful*: ``304`` with ``from_cache=True`` when a conditional GET
    revalidated a stored body, and ``None`` when no HTTP response was obtained
    at all (robots block, budget exhausted, transport failure after retry).
    """

    def __init__(
        self,
        status_code: int | None,
        text: str,
        url: str,
        *,
        error: str | None = None,
        from_cache: bool = False,
        blocked_by_robots: bool = False,
        budget_exhausted: bool = False,
        validators: dict | None = None,
    ):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.error = error
        self.from_cache = from_cache
        self.blocked_by_robots = blocked_by_robots
        self.budget_exhausted = budget_exhausted
        self.validators = validators or {}

    def json(self):
        return _json.loads(self.text)


def _client() -> httpx.Client:
    """The one place an ``httpx.Client`` is built in this module."""
    return httpx.Client(
        timeout=REQUEST_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


def _budget_for(url: str) -> tuple[str, int]:
    """Budget a request made outside any :func:`tool_budget` should spend."""
    bound = current_budget()
    if bound is not None:
        return bound
    host = (urlparse(url).netloc or "").lower()
    scope = f"fetch:{host}"
    try:
        get_politeness().reset_budget(scope)
    except Exception:
        pass
    return scope, FETCH_BUDGET_LIMIT


def _http_get(
    url: str,
    params: dict | None = None,
    *,
    budget_scope: str | None = None,
    budget_limit: int = FETCH_BUDGET_LIMIT,
    revalidate: bool = True,
) -> _Response:
    """GET ``url`` through the politeness layer.  Never raises.

    Order inside the layer: robots → budget → throttle (incl.
    ``Crawl-delay``) → conditional GET → send → ``429``/``503``/``504`` with
    ``Retry-After`` → stall detection.  The old version of this function
    retried on HTTP error statuses and then fell back to ``curl``; both are
    gone — retrying a soft-200 six times is what turned one typo into eighteen
    requests.

    Unless ``revalidate`` is false, the validators and raw body of an earlier
    response for the same URL are read from :class:`DocCache` and offered as
    ``If-None-Match`` / ``If-Modified-Since``, and a fresh ``200`` writes them
    back. Both are passed together or not at all (A3 §5.4).
    """
    if params:
        url = f"{url}?{urlencode(params)}"

    if budget_scope is None:
        budget_scope, budget_limit = _budget_for(url)

    validators: dict | None = None
    cached_body: bytes | None = None
    if revalidate:
        validators, cached_body = _revalidation_for(url)

    with _client() as client:
        response = get_politeness().get(
            client,
            url,
            validators=validators,
            cached_body=cached_body,
            budget_scope=budget_scope,
            budget_limit=budget_limit,
        )

    final_url = response.url or url
    if response.blocked_by_robots or response.error:
        error = response.error or f"request to {url} failed"
        return _Response(
            None,
            "",
            final_url,
            error=error,
            blocked_by_robots=response.blocked_by_robots,
            budget_exhausted=error.startswith(_BUDGET_ERROR_PREFIX),
            validators=response.validators or {},
        )

    if response.from_cache:
        # 304: the body is the one we already had, nothing was re-downloaded.
        # The response carries *fresh* validators, so write them back — keeping
        # the ones we sent would offer a stale ETag on the next revalidation.
        if revalidate:
            _remember_revalidation(url, response.validators or {}, response.text)
        return _Response(
            response.status_code,
            response.text,
            final_url,
            from_cache=True,
            validators=response.validators or {},
        )

    if response.status_code is None or response.status_code >= 400:
        return _Response(
            response.status_code,
            response.text,
            final_url,
            error=f"HTTP {response.status_code} from {url}",
            validators=response.validators or {},
        )

    if revalidate:
        _remember_revalidation(url, response.validators or {}, response.text)
    return _Response(
        response.status_code,
        response.text,
        final_url,
        validators=response.validators or {},
    )


# ---------------------------------------------------------------------------
# Small markdown helpers
# ---------------------------------------------------------------------------

def _clean_markdown(text: str) -> str:
    """Normalize whitespace runs in generated markdown."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _one_line(text: str) -> str:
    """Collapse all whitespace (incl. no-break spaces) to single spaces."""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _absolutize_links(element, base_url: str) -> None:
    """Rewrite relative hrefs to absolute URLs and drop noisy title attrs."""
    for anchor in element.find_all("a", href=True):
        try:
            anchor["href"] = urljoin(base_url, anchor["href"].strip())
        except (ValueError, TypeError):
            pass
        anchor.attrs.pop("title", None)


def _md(element_or_html, base_url: str | None = None) -> str:
    """Convert a BeautifulSoup element (or HTML string) to markdown."""
    if base_url is not None and hasattr(element_or_html, "find_all"):
        _absolutize_links(element_or_html, base_url)
    html = str(element_or_html)
    return _markdownify(html, heading_style="ATX", strip=["script", "style", "noscript"]).strip()


# ---------------------------------------------------------------------------
# JDK javadoc parsing
# ---------------------------------------------------------------------------

_JDK_SUMMARY_SECTIONS = {
    "nested-class-summary",
    "field-summary",
    "constructor-summary",
    "method-summary",
    "enum-constant-summary",
}
_JDK_DETAIL_SECTIONS = {
    "nested-class-details",
    "field-details",
    "constructor-details",
    "method-details",
}


def parse_jdk_html(html: str, url: str) -> dict:
    """Parse an Oracle javadoc class/interface page into clean markdown.

    Returns ``{"title": ..., "markdown": ...}``.  Raises ``ValueError`` when
    the document is not a javadoc class page (e.g. Oracle's redirect-to-home
    page for unknown classes).
    """
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main")
    if main is None:
        raise ValueError("not a javadoc class page (no <main> element found)")

    footer = main.find("footer")
    if footer is not None:
        footer.decompose()  # legal/copyright junk lives inside <main>

    h1 = main.find("h1")
    title = _one_line(h1.get_text()) if h1 else ""
    if not title:
        raise ValueError("not a javadoc class page (no <h1> title found)")

    parts: list[str] = [f"# {title}"]

    description = main.find("section", class_="class-description") or main.find(
        id="class-description"
    )
    if description is not None:
        desc_md = _jdk_description_md(description, url)
        if desc_md:
            parts.append(desc_md)

    for section in main.find_all("section"):
        classes = set(section.get("class") or [])
        if classes & _JDK_SUMMARY_SECTIONS:
            part = _jdk_summary_section_md(section)
        elif classes & _JDK_DETAIL_SECTIONS:
            part = _jdk_details_section_md(section, url)
        else:
            continue
        if part:
            parts.append(part)

    return {"title": title, "markdown": _clean_markdown("\n\n".join(parts))}


def _jdk_description_md(section, url: str) -> str:
    """Class/interface description: notes, type signature and prose."""
    parts: list[str] = []

    # The type signature becomes a java code fence (removed from the flow).
    signature = section.find("div", class_="type-signature")
    if signature is not None:
        sig_text = _one_line(signature.get_text())
        if sig_text:
            parts.append(f"```java\n{sig_text}\n```")
        signature.decompose()

    for hr in section.find_all("hr"):
        hr.decompose()

    body_md = _md(section, url)
    if body_md:
        parts.append(body_md)
    return "\n\n".join(parts)


def _jdk_summary_table_md(table) -> str:
    """Render a javadoc div-based summary table as a markdown table."""
    header_divs = table.select(".table-header")
    headers = [_one_line(d.get_text()) for d in header_divs] or ["Member", "Description"]

    cells = [
        d
        for d in table.find_all("div")
        if "table-header" not in (d.get("class") or [])
        and any(str(c).startswith("col-") for c in (d.get("class") or []))
    ]

    lines = [
        "| " + " | ".join(h.replace("|", "\\|") for h in headers) + " |",
        "|" + "---|" * len(headers),
    ]
    step = len(headers)
    for i in range(0, len(cells) - step + 1, step):
        row = []
        for cell in cells[i : i + step]:
            classes = [str(c) for c in (cell.get("class") or [])]
            if any(c.startswith("col-first") or c.startswith("col-second") for c in classes):
                # Modifier/type and member-name columns: keep as inline code.
                text = _one_line(cell.get_text()).replace("|", "\\|")
                row.append(f"`{text}`" if text else "")
            else:
                # Description column: prose, may contain inline <code>.
                text = _one_line(_md(cell)).replace("|", "\\|")
                row.append(text or "-")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _jdk_summary_section_md(section) -> str:
    """A *-summary section: declared members table plus inherited blocks."""
    h2 = section.find("h2")
    parts: list[str] = [f"## {h2.get_text(strip=True)}" if h2 else "## Summary"]

    primary_table = None
    for table in section.select(".summary-table"):
        if table.find_parent(class_="inherited-list") is None:
            primary_table = table
            break
    if primary_table is not None:
        parts.append(_jdk_summary_table_md(primary_table))

    for inherited in section.select(".inherited-list"):
        h3 = inherited.find("h3")
        heading = _one_line(h3.get_text()) if h3 else "Inherited members"
        block_parts = [f"### {heading}"]
        table = inherited.select_one(".summary-table")
        if table is not None:
            block_parts.append(_jdk_summary_table_md(table))
        parts.append("\n\n".join(block_parts))

    return "\n\n".join(parts)


def _jdk_details_section_md(section, url: str) -> str:
    """A *-details section: one subsection per member."""
    h2 = section.find("h2")
    parts: list[str] = [f"## {h2.get_text(strip=True)}" if h2 else "## Details"]
    for detail in section.select("section.detail"):
        part = _jdk_detail_md(detail, url)
        if part:
            parts.append(part)
    return "\n\n".join(parts)


def _jdk_detail_md(detail, url: str) -> str:
    """A single member detail: name, signature fence, description, notes."""
    h3 = detail.find("h3")
    name = _one_line(h3.get_text()) if h3 else "member"
    parts: list[str] = [f"### {name}"]

    signature = detail.find("div", class_="member-signature")
    if signature is not None:
        sig_text = _one_line(signature.get_text())
        if sig_text:
            parts.append(f"```java\n{sig_text}\n```")

    block = detail.find("div", class_="block")
    if block is not None:
        block_md = _md(block, url)
        if block_md:
            parts.append(block_md)

    notes = detail.find("dl", class_="notes")
    if notes is not None:
        notes_md = _md(notes, url)
        if notes_md:
            parts.append(notes_md)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Spring Boot reference parsing
# ---------------------------------------------------------------------------

def parse_spring_section_html(html: str, url: str) -> dict:
    """Parse a Spring Boot reference (Antora) page into clean markdown.

    Returns ``{"title": ..., "markdown": ...}``.  For section index pages the
    sidebar table of contents contributes a "## Contents" list with the
    subsections of the current page.  Raises ``ValueError`` when the document
    is not a Spring reference page.
    """
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article", class_="doc") or soup.find("article")
    if article is None:
        raise ValueError("not a Spring Boot reference page (no <article> found)")

    h1 = article.find("h1")
    title = _one_line(h1.get_text()) if h1 else ""
    if not title and soup.title is not None:
        title = _one_line(soup.title.get_text())

    # Work on a copy so breadcrumbs/pagination can be dropped safely.
    work = BeautifulSoup(str(article), "lxml")
    for selector in ("nav.breadcrumbs", ".breadcrumbs-container", "nav.pagination"):
        for element in work.select(selector):
            element.decompose()

    body_md = _md(work, url)

    parts: list[str] = []
    if body_md:
        parts.append(body_md)

    contents = _spring_section_contents(soup, url)
    if contents:
        parts.append("## Contents\n\n" + "\n".join(f"- {item}" for item in contents))

    return {"title": title or url, "markdown": _clean_markdown("\n\n".join(parts))}


def _spring_section_contents(soup, url: str) -> list[str]:
    """Markdown links for the subsections of the current page (if any)."""
    nav = soup.find("aside", class_="nav")
    if nav is None:
        return []
    current = nav.select_one("li.is-current-page")
    if current is None:
        return []
    items: list[str] = []
    for ul in current.find_all("ul", class_="nav-list", recursive=False):
        for li in ul.find_all("li", class_="nav-item", recursive=False):
            link = li.find("a", class_="nav-link")
            if link is None or not link.get("href"):
                continue
            label = _one_line(link.get_text())
            if not label:
                continue
            href = urljoin(url, str(link["href"]).strip())
            items.append(f"[{label}]({href})")
    return items


# ---------------------------------------------------------------------------
# Maven Central search
# ---------------------------------------------------------------------------

def parse_maven_search_json(data) -> list[dict]:
    """Extract artifact docs from a search.maven.org solrsearch JSON payload.

    Returns a list of ``{"group_id", "artifact_id", "version", "timestamp",
    "packaging", "description"}`` dicts in API order (empty list when there is
    nothing to parse).
    """
    if not isinstance(data, dict):
        return []
    response = data.get("response") or {}
    docs = response.get("docs") or []
    parsed: list[dict] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        parsed.append(
            {
                "group_id": doc.get("g"),
                "artifact_id": doc.get("a"),
                "version": doc.get("v"),
                "timestamp": doc.get("timestamp"),
                "packaging": doc.get("p"),
                "description": doc.get("description"),
            }
        )
    return parsed


def _version_key(version: str):
    """Best-effort sortable key for Maven version strings."""
    key = []
    for part in re.split(r"[.\-_+]", version or ""):
        if part.isdigit():
            key.append((1, int(part), ""))
        else:
            key.append((0, 0, part.lower()))
    return key


def fetch_maven_artifact(
    group_id: str | None, artifact_id: str, version: str | None = None
) -> dict:
    """Look up an artifact on Maven Central via the search API.

    Never raises; returns ``{"ok": False, "error": ...}`` on any failure.
    """
    artifact = (artifact_id or "").strip()
    group = (group_id or "").strip() or None
    if not artifact:
        return {"ok": False, "error": "artifact_id is required"}

    query = f'g:"{group}" AND a:"{artifact}"' if group else f'a:"{artifact}"'
    params = {"q": query, "core": "gav", "rows": 20, "wt": "json"}
    response = _http_get(MAVEN_SEARCH_URL, params=params)
    if response.error:
        failure: dict = {
            "ok": False,
            "error": f"Maven Central request failed: {response.error}",
        }
        if response.budget_exhausted:
            failure["budget_exhausted"] = True
        return failure

    # A revalidated 304 carries the cached body, so status alone is not the
    # test (A3 §5.1) — ``from_cache`` is what says "we have usable content".
    if not response.from_cache and response.status_code != 200:
        return {
            "ok": False,
            "error": f"Maven Central returned HTTP {response.status_code}",
            "suggestion": "The search API is occasionally flaky; retry or check the coordinates.",
        }
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "error": "Maven Central returned a non-JSON response"}

    all_docs = parse_maven_search_json(data)
    if not all_docs:
        where = f"{group}:{artifact}" if group else artifact
        return {
            "ok": False,
            "error": "artifact not found on Maven Central",
            "suggestion": (
                f"No results for '{where}'. Verify the coordinates, or retry without a "
                f"group id: fetch_maven_artifact(None, {artifact!r})"
            ),
        }

    if version is not None:
        wanted = str(version).strip()
        docs = [d for d in all_docs if d["version"] == wanted]
        if not docs:
            available = sorted(
                {d["version"] for d in all_docs if d["version"]},
                key=_version_key,
                reverse=True,
            )[:10]
            where = f"{group}:{artifact}" if group else artifact
            return {
                "ok": False,
                "error": f"version '{wanted}' not found for {where}",
                "suggestion": (
                    "Available versions (newest first): " + ", ".join(available)
                    if available
                    else "No versions returned by the search API."
                ),
            }
    else:
        docs = all_docs

    # The API orders by relevance, not recency; sort newest-first ourselves.
    docs.sort(key=lambda d: (d.get("timestamp") or 0), reverse=True)
    selected = docs[0]

    versions: list[str] = []
    for doc in docs:
        v = doc.get("version")
        if v and v not in versions:
            versions.append(v)

    sel_group = selected.get("group_id") or group
    sel_artifact = selected.get("artifact_id") or artifact
    result: dict = {
        "ok": True,
        "group_id": sel_group,
        "artifact_id": sel_artifact,
        "version": selected.get("version"),
        "versions": versions,
        "description": next((d["description"] for d in docs if d.get("description")), None),
    }
    if sel_group:
        result["url"] = f"https://central.sonatype.com/artifact/{sel_group}/{sel_artifact}"
        result["javadoc_url"] = f"https://www.javadoc.io/doc/{sel_group}/{sel_artifact}"
    else:
        result["url"] = f"https://search.maven.org/search?q=a:{sel_artifact}"
        result["javadoc_url"] = None
    return result


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------

def fetch_jdk_class(fully_qualified: str, module: str = "java.base") -> dict:
    """Fetch and parse a JDK class/interface javadoc page. Never raises."""
    fq = (fully_qualified or "").strip()
    if not fq or "." not in fq or "/" in fq or any(ch.isspace() for ch in fq):
        return {
            "ok": False,
            "error": (
                f"invalid fully-qualified class name: {fully_qualified!r} "
                "(expected e.g. 'java.util.List')"
            ),
        }
    package, _, simple_name = fq.rpartition(".")
    if not package or not simple_name:
        return {
            "ok": False,
            "error": (
                f"invalid fully-qualified class name: {fully_qualified!r} "
                "(expected package.ClassName)"
            ),
        }

    package_path = package.replace(".", "/")
    url = f"{JDK_API_BASE}/{module}/{package_path}/{simple_name}.html"
    suffix = f"/{module}/{package_path}/{simple_name}.html"

    response = _http_get(url)
    if response.error:
        failure = {"ok": False, "error": response.error}
        if response.blocked_by_robots:
            failure["suggestion"] = (
                "docs.oracle.com robots.txt disallows this path — see "
                "https://docs.oracle.com/robots.txt (older JDK lines, /javase/12/ "
                "to /javase/20/, are disallowed); use the current javase line "
                "or java_search() for a supported class"
            )
        if response.budget_exhausted:
            failure["budget_exhausted"] = True
        return failure

    if not response.from_cache and (
        response.status_code is None or response.status_code >= 400
    ):
        return {"ok": False, "error": f"HTTP {response.status_code} for {url}"}

    final_url = str(response.url)
    if not final_url.endswith(suffix):
        return {
            "ok": False,
            "error": (
                f"class '{fq}' not found in module '{module}' "
                f"(server redirected to {final_url})"
            ),
        }

    try:
        parsed = parse_jdk_html(response.text, url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "url": url, **parsed}


def fetch_spring_boot_section(section: str = "index") -> dict:
    """Fetch and parse a Spring Boot reference section page. Never raises."""
    sec = (section or "index").strip().strip("/") or "index"
    if sec == "index":
        url = f"{SPRING_REFERENCE_BASE}/index.html"
    else:
        url = f"{SPRING_REFERENCE_BASE}/{sec}/index.html"

    response = _http_get(url)
    if response.error:
        failure = {"ok": False, "error": response.error}
        if response.blocked_by_robots:
            failure["suggestion"] = (
                "docs.spring.io robots.txt disallows this path "
                "(https://docs.spring.io/robots.txt); use a section listed in "
                "java_search('spring ...')"
            )
        if response.budget_exhausted:
            failure["budget_exhausted"] = True
        return failure

    if not response.from_cache and (
        response.status_code is None or response.status_code >= 400
    ):
        return {
            "ok": False,
            "error": f"HTTP {response.status_code} for {url} (section '{sec}' may not exist)",
        }

    try:
        parsed = parse_spring_section_html(response.text, url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "url": url, "section": sec, **parsed}
