"""Spring documentation layer: reference docs, guides, Initializr and releases.

Why this module exists
----------------------
``java_docs("spring:using")`` can return *one known section* of the Spring Boot
reference, but it cannot answer "where is auto-configuration documented?",
"which starter do I add for JPA?" or "what is the current Spring Boot release?".
This module adds the four data sources those questions need — the Spring Boot
reference **nav tree**, the spring.io **guide catalogue**, Spring Initializr's
**metadata + generated build files** and GitHub **releases** — and the parsers
and ranking that turn them into tool results.

It lives beside :mod:`java_spring_mcp.search` rather than inside
:mod:`java_spring_mcp.fetchers`: same shape (fetch → pure parser → cached index
→ offline ranking), and it reuses the existing Spring code path wherever one
exists — :func:`java_spring_mcp.fetchers.fetch_spring_boot_section` for a plain
section, :func:`java_spring_mcp.fetchers.parse_spring_section_html` for any
reference page, :func:`java_spring_mcp.fetchers._http_get` for every request.

MEASURED LIVE (2026-10-07, UA ``java-spring-mcp/0.2``)
------------------------------------------------------
robots.txt, re-verified before any of this was written:

* ``https://spring.io/robots.txt`` → ``200``: ``User-agent: *`` / ``Allow: /``,
  plus ``Sitemap: https://spring.io/sitemap-index.xml`` and ``Host: https://spring.io``.
* ``https://docs.spring.io/robots.txt`` → ``200``: ``User-agent: *`` with
  ``crawl-delay: 1`` **and** a second ``User-agent: *`` group carrying
  ``Disallow: /autorepo/``.  Both groups apply (the layer merges them), the
  delay is waited out, and no path in this module ever reaches ``/autorepo/``.
* ``https://start.spring.io/robots.txt`` → ``404`` (a JSON error body, not a
  robots file).  No file means no rule: allowed.
* ``https://api.github.com/robots.txt`` → ``404``.  Allowed — but the un-
  authenticated REST core quota is ``{"limit": 60, "remaining": 57}`` at the
  moment of measuring, i.e. **60 requests per hour per IP**.  GitHub releases
  are therefore cached for a day and one ``spring_versions`` call spends at
  most two of them.

Endpoints, each confirmed by an actual request:

* Reference docs are Antora pages.  ``/spring-boot/reference/index.html``
  (``200``, 82 644 B) carries the whole documentation tree in
  ``<aside class="nav">`` — 302 ``li.nav-item`` elements with ``data-depth``
  and a ``nav-link`` — and states the version it serves in
  ``<span class="version">4.1.1</span>``.  That single page is the concept
  index; no crawl is needed to build it.
* Versioned docs exist: ``/spring-boot/4.1/reference/index.html`` and
  ``/spring-boot/3.4/reference/index.html`` both ``200``.  The unversioned
  ``/reference/`` prefix is the **latest** line (measured: its own links point
  at ``/spring-boot/4.1/…``).
* ``https://spring.io/guides`` is rendered client-side: the served HTML
  (401 139 B) contains **two** links whose href contains ``/guides`` — both
  navigation — and zero guide cards.  The catalogue is in the Gatsby data file
  ``https://spring.io/page-data/guides/page-data.json`` (``200``, 17 902 B,
  ``result.data.guides.nodes``: 68 entries with ``title``, ``description``,
  ``type``, ``path``, ``category``).  The sitemap
  (``sitemap-index.xml`` → ``sitemap-0.xml``, 1 098 181 B, 7 431 URLs, 71 of
  them ``/guides/``) is the fallback and carries URLs only.
* Guide *detail* pages are server-rendered (``/guides/gs/spring-boot/``,
  430 261 B, ``<title>Getting Started | Building an Application with Spring
  Boot</title>``, ``div.guide`` with AsciiDoc blocks) — so a guide can be
  fetched as markdown even though its index cannot be scraped.
* Initializr content negotiation: ``Accept: application/vnd.initializr.v2.1+json``
  → ``200 application/vnd.initializr.v2.1+json`` (75 760 B);
  ``Accept: application/hal+json`` → the **same body** with
  ``application/hal+json``; ``Accept: */*`` → v2.1 as well.  We ask for v2.1
  explicitly because that is the shape we parse.  ``bootVersion.default`` is
  ``4.1.1.RELEASE``.
* ``/dependencies`` (``200``, 24 961 B) is a *different* shape — a flat map
  keyed by dependency id — and ``?bootVersion=3.5.16.RELEASE`` answers
  ``400``: Initializr only offers the versions it can generate.
* Generated build files are the authoritative coordinate source:
  ``/pom.xml?type=maven-build&dependencies=data-jpa,web`` (``200``, 1 655 B)
  emits ``spring-boot-starter-data-jpa`` **and** ``spring-boot-starter-webmvc``
  — not ``spring-boot-starter-web``.  Deriving ``spring-boot-starter-<id>``
  from the catalogue id would be wrong for Spring Boot 4.1, which is exactly
  why :func:`fetch_initializr_build_files` asks Initializr to write the build
  file instead of guessing it.  ``/build.gradle?type=gradle-build`` needs the
  **plain** version (``bootVersion=4.1.1`` → ``200``); ``4.1.1.RELEASE`` →
  ``500 … Bom 'spring-boot-dependencies:4.1.1.RELEASE' could not be resolved``.
* GitHub releases: ``/repos/spring-projects/spring-boot/releases?per_page=100``
  → ``200``, 1 111 525 B, 100 releases of which 76 are stable; newest stable
  ``v4.1.1`` (2026-08-20), newest any ``v4.2.0-M2``.  Body section headings,
  counted over those 100 bodies: ``:lady_beetle: Bug Fixes`` 99,
  ``:hammer: Dependency Upgrades`` 98, ``:notebook_with_decorative_cover:
  Documentation`` 97, ``:heart: Contributors`` 97, ``:star: New Features`` 38,
  ``:warning: Noteworthy Changes`` 6, ``:warning: Noteworthy`` 6,
  ``:warning: Attention Required`` 4, ``❗ Noteworthy Changes`` 1,
  ``⚠️ Noteworthy Changes`` 1.  No release body has a ``Deprecations``
  heading, so deprecation bullets are found by keyword, not by heading.
* Version truth, because the third-party server this layer replaces claims
  4.1.1 while ``maven_package`` claims 3.5.3: ``repo1.maven.org/…/spring-boot/
  maven-metadata.xml`` and the same for ``spring-boot-starter-web`` each list
  **294 versions, ``<latest>4.2.0-M2</latest>``**, newest stable 4.1.1 — while
  ``search.maven.org/solrsearch?core=gav`` returns **3.5.3** as newest for
  *both* artifacts with byte-identical version lists (``numFound`` 252).  The
  search index is stale and artifact-blind; 4.1.1 is real.  See README.

Design rules
------------
* No function here calls an MCP tool, and no function raises: every failure is
  ``{"ok": False, "error": …, "suggestion": …}``.  Parsers raise
  ``ValueError`` only to say "this is not the document I expected", which the
  fetcher converts into that dict.
* Every document is cached in :class:`~java_spring_mcp.cache.DocCache` keyed by
  URL: reference pages and guides 7 days, Initializr metadata and GitHub
  releases 1 day.  A cached read issues **zero** requests, so a second identical
  tool call is a cache hit and spends no quota.
* Requests go through :func:`java_spring_mcp.fetchers._http_get`, i.e. the
  politeness layer (robots, ``Crawl-delay: 1`` on docs.spring.io, throttle,
  budgets, conditional GET), inside the enclosing tool call's budget.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlencode, urlparse

from bs4 import BeautifulSoup

from .cache import DocCache
from .fetchers import (
    SPRING_REFERENCE_BASE,
    _http_get,
    _md,
    parse_spring_section_html,
)

__all__ = [
    "SPRING_DOCS_BASE",
    "SPRING_SITE_BASE",
    "SPRING_GUIDES_URL",
    "SPRING_GUIDES_PAGE_DATA_URL",
    "SPRING_SITEMAP_INDEX_URL",
    "START_SPRING_BASE",
    "GITHUB_API_BASE",
    "GITHUB_REPOS",
    "INITIALIZR_ACCEPT",
    "GITHUB_ACCEPT",
    "CONCEPT_INDEX_KEY",
    "HEADINGS_KEY",
    "GUIDES_INDEX_KEY",
    "SPRING_PAGE_TTL",
    "METADATA_TTL",
    "HEADING_PAGE_LIMIT",
    "reference_base",
    "detect_docs_version",
    "build_concept_index",
    "load_concept_index",
    "load_heading_store",
    "save_heading_store",
    "parse_reference_nav",
    "parse_page_headings",
    "excerpt_after_heading",
    "fetch_reference_page",
    "resolve_reference_url",
    "fetch_reference_url",
    "load_guide_catalogue",
    "guide_url_for",
    "parse_guide_html",
    "parse_guides_page_data",
    "parse_sitemap_guides",
    "fetch_guide_page",
    "fetch_initializr_metadata",
    "parse_initializr_metadata",
    "fetch_initializr_build_files",
    "parse_pom_dependencies",
    "parse_gradle_dependencies",
    "plain_boot_version",
    "maven_snippet",
    "gradle_snippet",
    "fetch_github_releases",
    "parse_release",
    "filter_release_sections",
    "normalize_version_tag",
    "rank_entries",
    "rank_headings",
    "rank_dependencies",
    "candidate_pages",
]

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

#: Root of the Spring Boot docs site.  ``{base}/reference/…`` is the reference;
#: ``{base}/{version}/reference/…`` is the same tree for an older line.
SPRING_DOCS_BASE = "https://docs.spring.io/spring-boot"
SPRING_SITE_BASE = "https://spring.io"
SPRING_GUIDES_URL = f"{SPRING_SITE_BASE}/guides"
#: Measured: the guide index is client-side rendered, this is where its data is.
SPRING_GUIDES_PAGE_DATA_URL = f"{SPRING_SITE_BASE}/page-data/guides/page-data.json"
SPRING_SITEMAP_INDEX_URL = f"{SPRING_SITE_BASE}/sitemap-index.xml"
START_SPRING_BASE = "https://start.spring.io"
GITHUB_API_BASE = "https://api.github.com"

#: ``Accept`` that makes Initializr serve the v2.1 metadata shape (measured:
#: ``*/*`` and ``application/hal+json`` return the same body, but asking
#: explicitly is what pins the shape we parse).
INITIALIZR_ACCEPT = "application/vnd.initializr.v2.1+json"
GITHUB_ACCEPT = "application/vnd.github+json"

#: Projects whose releases ``spring_versions`` can read.  Every entry is a real
#: ``spring-projects`` repository reached through the same code path.
GITHUB_REPOS = {
    "spring-boot": "spring-projects/spring-boot",
    "spring-framework": "spring-projects/spring-framework",
    "spring-security": "spring-projects/spring-security",
    "spring-data-jpa": "spring-projects/spring-data-jpa",
    "spring-batch": "spring-projects/spring-batch",
    "spring-ai": "spring-projects/spring-ai",
}

# ---------------------------------------------------------------------------
# Cache keys and TTLs
# ---------------------------------------------------------------------------

CONCEPT_INDEX_KEY = "spring-concept-index"
#: Harvested in-page headings, keyed by page URL.  Separate from the nav index
#: on purpose: enriching it must not silently extend the nav index's TTL.
HEADINGS_KEY = "spring-headings"
GUIDES_INDEX_KEY = "spring-guides-index"

#: Reference pages and guide pages: one week (same as the server's Spring TTL).
SPRING_PAGE_TTL = 604800
#: Initializr metadata and GitHub releases: one day.  For GitHub this is not a
#: performance choice — 60 unauthenticated requests per hour is the ceiling.
METADATA_TTL = 86400

#: How many reference pages one ``spring_search_concepts`` call may fetch to
#: read their in-page headings.  Chosen so the whole call stays inside
#: :data:`~java_spring_mcp.fetchers.FETCH_BUDGET_LIMIT` even on a cold index:
#: index page (1) + these (4) = 5 of 6, with one unit of reserve for a retry.
#: With ``docs.spring.io``'s ``crawl-delay: 1`` this is also the wall-clock cost
#: of a cold search: about five seconds, once per week.
HEADING_PAGE_LIMIT = 4

_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache() -> DocCache | None:
    """DocCache, or ``None`` when it cannot be opened (a cache is never fatal)."""
    try:
        return DocCache()
    except Exception:
        return None


def _error(message: str, suggestion: str, extra: dict | None = None) -> dict:
    """The house failure shape: ``{"ok": False, "error", "suggestion"}``.

    ``extra`` adds context a caller can act on (for instance the list of section
    names that *do* exist when the requested one does not).
    """
    out: dict = {"ok": False, "error": message, "suggestion": suggestion}
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Cached fetch (read-through DocCache → politeness layer)
# ---------------------------------------------------------------------------

def _fetch_cached(
    url: str,
    ttl: float,
    *,
    headers: dict | None = None,
    revalidate: bool = True,
) -> dict:
    """Fetch ``url`` through the politeness layer with a DocCache read-through.

    Same semantics as ``server._cache_get`` but shared here because the Spring
    layer caches JSON (Initializr, GitHub, Gatsby page data) as well as HTML.
    A live cache entry means **no request at all**, which is what keeps a
    ``spring_versions`` call from spending GitHub's hourly quota.  The
    politeness layer's own conditional GET still applies on a cache miss.

    Returns ``{"ok": True, "url", "text", "cached": bool}`` or an error dict.
    Failures are never cached.
    """
    cache = _cache()
    if cache is not None:
        entry = cache.get_entry(url)
        if entry is not None and entry.get("body"):
            return {"ok": True, "url": url, "text": entry["body"], "cached": True}

    response = _http_get(url, headers=headers, revalidate=revalidate)
    if response.error:
        failure = _error(
            f"fetch failed for {url}: {response.error}",
            f"check that {urlparse(url).netloc} is reachable, then retry",
        )
        if response.budget_exhausted:
            failure["budget_exhausted"] = True
        return failure

    if cache is not None and not response.from_cache:
        validators = response.validators or {}
        # Store the body as the value *and* keep the validators + raw body, so
        # the next call either reads the value or revalidates with a conditional
        # GET.  DocCache.set takes etag/last_modified/body separately (cache.py
        # :meth:`DocCache.set`); passing them together is what keeps one row.
        cache.set(
            url,
            response.text,
            ttl,
            etag=validators.get("etag"),
            last_modified=validators.get("last_modified"),
            body=response.text,
        )
    return {
        "ok": True,
        "url": response.url or url,
        "text": response.text,
        "cached": bool(response.from_cache),
    }


def _cached_json(key: str, ttl: float, builder) -> dict:
    """Return a built index from the cache, calling ``builder()`` on a miss."""
    cache = _cache()
    if cache is not None:
        stored = cache.get(key)
        if stored is not None:
            try:
                value = json.loads(stored)
            except (TypeError, ValueError):
                value = None
            if value is not None:
                value["cached"] = True
                return value
    built = builder()
    if not built.get("ok", True):
        return built
    if cache is not None:
        cache.set_value(key, json.dumps(built, ensure_ascii=False), ttl)
    built["cached"] = False
    return built


# ---------------------------------------------------------------------------
# Reference docs: URL building and nav parsing
# ---------------------------------------------------------------------------

def reference_base(version: str | None = None) -> str:
    """Base URL of the Spring Boot reference for ``version``.

    ``None`` → the unversioned prefix, which the site serves as the **latest**
    line (measured: ``/spring-boot/reference/index.html`` links to
    ``/spring-boot/4.1/…``).  A version must look like ``4.1`` or ``4.1.1``;
    anything else is rejected here rather than turned into a 404 on the wire.
    """
    if version is None:
        return SPRING_REFERENCE_BASE
    cleaned = str(version).strip().lstrip("vV")
    if not _VERSION_RE.match(cleaned):
        raise ValueError(
            f"version {version!r} must look like '4.1' or '4.1.1' "
            "(a major.minor line, optionally a patch)"
        )
    return f"{SPRING_DOCS_BASE}/{cleaned}/reference"


def parse_reference_nav(html: str, base_url: str) -> list[dict]:
    """Parse the Antora navigation tree of a Spring Boot docs page.

    Returns one entry per nav link::

        {"title", "url", "path", "slug", "section", "depth", "parent"}

    ``section`` is the reference section for reference pages (``using``,
    ``web``, ``data``, …) and the top-level directory for the rest
    (``appendix``, ``tutorial``, ``specification``).  Raises ``ValueError`` when
    the page has no ``<aside class="nav">``, i.e. it is not a docs page.
    """
    soup = BeautifulSoup(html, "lxml")
    nav = soup.find("aside", class_="nav") or soup.find("nav", class_="nav-menu")
    if nav is None:
        raise ValueError("not a Spring Boot docs page (no <aside class=\"nav\"> found)")

    entries: list[dict] = []
    seen: set[str] = set()
    for item in nav.select("li.nav-item"):
        link = item.find("a", recursive=False)
        if link is None:
            continue  # a depth-0 wrapper <li> that only groups children
        href = (link.get("href") or "").strip()
        title = " ".join(link.get_text(" ", strip=True).split())
        if not href or not title:
            continue
        url = urljoin(base_url, href)
        parts = urlparse(url)
        if parts.netloc != "docs.spring.io":
            continue
        path = parts.path or "/"
        if path in seen:
            continue
        seen.add(path)
        entries.append(
            {
                "title": title,
                "url": url,
                "path": path,
                "slug": path.rsplit("/", 1)[-1][: -len(".html")] if path.endswith(".html") else path.rsplit("/", 1)[-1],
                "section": _section_of(path),
                "depth": _int_or(item.get("data-depth"), 0),
                "parent": _parent_title(item),
            }
        )
    return entries


def _section_of(path: str) -> str:
    """``/spring-boot/reference/using/x.html`` → ``using``; ``/spring-boot/appendix/…`` → ``appendix``."""
    tail = path[len("/spring-boot/") :] if path.startswith("/spring-boot/") else path
    if tail.startswith("reference/"):
        tail = tail[len("reference/") :]
    head = tail.split("/", 1)[0]
    return head or "index"


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parent_title(item) -> str:
    """Title of the nearest ancestor nav item that has a link."""
    parent = item.find_parent("li", class_="nav-item")
    while parent is not None:
        link = parent.find("a", recursive=False)
        if link is not None:
            title = " ".join(link.get_text(" ", strip=True).split())
            if title:
                return title
        parent = parent.find_parent("li", class_="nav-item")
    return ""


def detect_docs_version(html: str) -> str:
    """The version the docs page says it serves (``<span class="version">4.1.1</span>``).

    Empty string when the page does not say — the caller then falls back to the
    version it asked for.
    """
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("aside.nav .context .version") or soup.select_one(".version")
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def build_concept_index(version: str | None = None) -> dict:
    """Build the Spring Boot concept index from one page: the reference nav.

    One request (plus robots.txt on a cold robots cache) yields every section,
    subpage, appendix and how-to page of the documentation with its title —
    measured 302 nav items on ``/spring-boot/reference/index.html``.  This is
    deliberately *not* a crawl: ``docs.spring.io`` declares ``crawl-delay: 1``,
    so crawling 300 pages would cost five minutes per index rebuild.
    """
    try:
        base = reference_base(version)
    except ValueError as exc:
        return _error(str(exc), "omit 'version' for the latest docs, or pass e.g. '4.1'")

    url = f"{base}/index.html"
    fetched = _fetch_cached(url, SPRING_PAGE_TTL)
    if not fetched["ok"]:
        return fetched
    try:
        entries = parse_reference_nav(fetched["text"], fetched["url"])
    except ValueError as exc:
        return _error(str(exc), f"fetch {url} and check it is the Spring Boot reference index")
    if not entries:
        return _error(
            f"no nav entries found at {url}",
            "the docs navigation may have changed; report it",
        )
    return {
        "ok": True,
        "url": fetched["url"],
        "base_url": base,
        "requested_version": version,
        "version": detect_docs_version(fetched["text"]) or (version or ""),
        "entries": entries,
        "entry_count": len(entries),
        "built_at": _now_iso(),
        "cached": fetched["cached"],
    }


def _concept_index_key(version: str | None) -> str:
    return CONCEPT_INDEX_KEY if version is None else f"{CONCEPT_INDEX_KEY}:{version}"


def load_concept_index(
    version: str | None = None, *, force_refresh: bool = False, max_age_seconds: float = SPRING_PAGE_TTL
) -> dict:
    """Cached access to the concept index.  Never raises.

    A fresh-enough cached index costs nothing; a missing or stale one is
    rebuilt from the reference nav.  A *failed* rebuild is reported, and an
    older cached index is served as ``stale`` when one exists — the same
    fallback :func:`java_spring_mcp.search.load_index` uses.
    """
    key = _concept_index_key(version)
    cache = _cache()
    if cache is not None and not force_refresh:
        entry = cache.get_entry(key)
        if entry is not None:
            try:
                index = json.loads(entry["value"])
            except (TypeError, ValueError):
                index = None
            if index:
                index["cached"] = True
                return index

    built = build_concept_index(version)
    if built.get("ok"):
        if cache is not None:
            cache.set_value(key, json.dumps(built, ensure_ascii=False), max_age_seconds)
        return built

    if cache is not None:
        stale = cache.get_entry(key, allow_expired=True)
        if stale is not None and stale.get("value"):
            try:
                index = json.loads(stale["value"])
            except (TypeError, ValueError):
                index = None
            if index:
                index["stale"] = True
                index["stale_reason"] = built["error"]
                index["cached"] = True
                return index
    return built


def load_heading_store() -> dict:
    """Harvested page notes: ``{url: {"headings": […], "summary": "…"}, …}``.

    The persisted row is a wrapper (``{"headings": …, "updated_at": …}``) so the
    store can say when it was last extended; callers work on the inner mapping
    directly, which is what makes "have I already read this page?" a plain
    membership test on the URL.  The summary is stored next to the headings so a
    warm search quotes the page just as a cold one does — otherwise the second
    call of a query would answer with bare titles.
    """
    cache = _cache()
    if cache is None:
        return {}
    stored = cache.get(HEADINGS_KEY)
    if stored is None:
        return {}
    try:
        wrapper = json.loads(stored)
    except (TypeError, ValueError):
        return {}
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("headings"), dict):
        return {}
    store = {}
    for url, note in wrapper["headings"].items():
        if isinstance(note, list):  # an older row stored the heading list directly
            store[url] = {"headings": note, "summary": ""}
        elif isinstance(note, dict) and isinstance(note.get("headings"), list):
            store[url] = {"headings": note["headings"], "summary": str(note.get("summary") or "")}
    return store


def save_heading_store(headings: dict) -> None:
    """Persist harvested headings (a write failure must never fail a search)."""
    cache = _cache()
    if cache is None:
        return
    payload = {"headings": headings, "updated_at": _now_iso()}
    try:
        cache.set_value(HEADINGS_KEY, json.dumps(payload, ensure_ascii=False), SPRING_PAGE_TTL)
    except Exception:
        pass


def parse_page_headings(html: str, url: str) -> list[dict]:
    """In-page headings of a reference page: ``{"level", "text", "anchor", "excerpt"}``.

    ``data/sql.html`` measured 28 headings, including
    ``h2 id="data.sql.jpa-and-spring-data" → "JPA and Spring Data JPA"`` — the
    reason a concept search can find *JPA* although no page is titled "JPA".

    Each heading carries the ~240 characters of prose that follow it, captured
    once at harvest time and stored with the heading: a concept search can then
    quote an excerpt without ever re-reading the page.
    """
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article", class_="doc") or soup.find("article") or soup.find("main")
    if article is None:
        raise ValueError(f"not a Spring Boot docs page (no <article> found at {url})")
    headings: list[dict] = []
    for node in article.select("h2, h3, h4"):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text:
            continue
        headings.append(
            {
                "level": node.name,
                "text": text,
                "anchor": node.get("id") or "",
                "excerpt": _heading_excerpt(node),
            }
        )
    return headings


def _heading_excerpt(node, limit: int = 240) -> str:
    """Prose directly under a heading, cut at ``limit`` characters.

    Antora lays a docs page out flat — heading, paragraph, list, heading — so
    following siblings are the section body.  The next heading ends the section;
    a code block or table is skipped rather than quoted, because a snippet of
    Java is not an excerpt of what a page *says*.
    """
    chunks: list[str] = []
    total = 0
    for sibling in node.next_siblings:
        name = getattr(sibling, "name", None)
        if name in ("h1", "h2", "h3", "h4"):
            break
        if name == "p":
            text = " ".join(sibling.get_text(" ", strip=True).split())
        elif name in ("ul", "ol"):
            first = sibling.find(["li"], recursive=False)
            text = " ".join(first.get_text(" ", strip=True).split()) if first is not None else ""
        elif name == "div":
            classes = " ".join(sibling.get("class") or [])
            if "admonition" not in classes and "listingblock" in classes:
                continue
            text = " ".join(sibling.get_text(" ", strip=True).split())
        else:
            continue
        if not text:
            continue
        chunks.append(text)
        total += len(text)
        if total >= limit:
            break
    excerpt = " ".join(chunks)
    if len(excerpt) > limit:
        excerpt = excerpt[:limit].rsplit(" ", 1)[0] + "…"
    return excerpt


def page_lede(html: str, limit: int = 240) -> str:
    """A docs page\'s own opening paragraph, which often precedes any heading.

    Section landing pages (``actuator/index.html``) have no ``h2`` at all — just
    an ``h1`` and a summary paragraph — so a heading-based summary comes back
    empty for exactly the pages a concept search most wants to describe.
    """
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article", class_="doc") or soup.find("article") or soup.find("main")
    if article is None:
        return ""
    for node in article.find_all("p"):
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) < 40:
            continue  # skip breadcrumbs and one-word stubs
        return text[:limit] + "…" if len(text) > limit else text
    return ""


def excerpt_after_heading(html: str, anchor: str = "") -> str:
    """The excerpt stored under ``anchor``, or the page's opening excerpt.

    With no ``anchor`` this is the first heading's body — the page's own summary.
    Returns "" when the page has no headings or no prose under them.
    """
    try:
        headings = parse_page_headings(html, "")
    except ValueError:
        return ""
    if not headings:
        return ""
    if anchor:
        for heading in headings:
            if heading["anchor"] == anchor:
                return heading["excerpt"]
        return ""
    for heading in headings:
        if heading["excerpt"]:
            return heading["excerpt"]
    return ""


def fetch_reference_page(path: str, version: str | None = None) -> dict:
    """Fetch one reference page (``"web"``, ``"data/sql"``, ``"howto/webserver"``).

    Reuses the existing Spring code path when it applies: a single-segment path
    with no explicit version goes through
    :func:`java_spring_mcp.fetchers.fetch_spring_boot_section`, so ``spring_
    reference("web")`` and ``java_docs("spring:web")`` share one cache entry.
    Everything else (subsections, versioned lines, appendix pages) is fetched
    here and parsed with the same
    :func:`java_spring_mcp.fetchers.parse_spring_section_html`.

    Returns ``{"ok", "url", "path", "version", "title", "markdown", "headings",
    "cached"}`` or an error dict.
    """
    cleaned = (path or "").strip().strip("/")
    if not cleaned:
        return _error(
            "no section given",
            "e.g. spring_reference('web'), spring_reference('data/sql') or spring_reference('howto')",
        )
    cleaned = re.sub(r"\.html$", "", cleaned)
    try:
        base = reference_base(version)
    except ValueError as exc:
        return _error(str(exc), "omit 'version' for the latest docs, or pass e.g. '4.1'")

    if version is None and "/" not in cleaned:
        fetched = _fetch_spring_section(cleaned)
    else:
        url = f"{base}/{cleaned}.html"
        raw = _fetch_cached(url, SPRING_PAGE_TTL)
        if not raw["ok"]:
            return raw
        try:
            parsed = parse_spring_section_html(raw["text"], raw["url"])
        except ValueError as exc:
            return _error(
                f"{exc} (path '{cleaned}')",
                "list valid paths with spring_search_concepts('<your topic>')",
            )
        fetched = {
            "ok": True,
            "url": raw["url"],
            "cached": raw["cached"],
            "html": raw["text"],
            **parsed,
        }

    if not fetched.get("ok"):
        return fetched

    headings: list[dict] = []
    lede = ""
    html = fetched.get("html") or _page_html(fetched["url"]) or ""
    if html:
        try:
            headings = parse_page_headings(html, fetched["url"])
        except ValueError:
            headings = []
        lede = page_lede(html)
    return {
        "ok": True,
        "url": fetched["url"],
        "path": cleaned,
        "version": version or "latest",
        "title": fetched.get("title") or cleaned,
        "markdown": fetched.get("markdown") or "",
        "headings": headings,
        "lede": lede,
        "cached": bool(fetched.get("cached")),
    }


# ``fetch_reference_page`` parses markdown through ``parse_spring_section_html``
# already; to list headings it needs the raw HTML again.  When the page came
# through the existing fetcher it is not returned to us, so it is read back from
# the cache the politeness layer wrote — no second request.
def _page_html(url: str) -> str | None:
    cache = _cache()
    if cache is None:
        return None
    entry = cache.get_entry(url)
    return entry["body"] if entry is not None and entry.get("body") else None


def _fetch_spring_section(section: str) -> dict:
    """Delegate to the existing Spring reference fetcher (same cache key)."""
    from .fetchers import fetch_spring_boot_section

    result = fetch_spring_boot_section(section)
    if not result.get("ok"):
        error = result.get("error") or f"Spring section '{section}' could not be fetched"
        return _error(
            error,
            "spring_search_concepts('<topic>') lists the sections that do exist",
        )
    cache = _cache()
    cached = False
    html: str | None = None
    if cache is not None:
        entry = cache.get_entry(result.get("url") or "")
        if entry is not None and entry.get("body"):
            cached = True
            html = entry["body"]
    return {
        "ok": True,
        "url": result.get("url"),
        "cached": cached,
        "html": html,
        "title": result.get("title") or section,
        "markdown": result.get("markdown") or "",
    }


# ---------------------------------------------------------------------------
# Guides
# ---------------------------------------------------------------------------

def parse_guides_page_data(text: str) -> list[dict]:
    """Parse spring.io's Gatsby page-data for the guide catalogue.

    ``result.data.guides.nodes`` → ``{"title", "url", "slug", "type",
    "category", "description"}``.  Returns ``[]`` for any other shape (never
    raises), which is what makes the sitemap fallback reachable.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    nodes = (
        data.get("result", {}).get("data", {}).get("guides", {}).get("nodes")
        if isinstance(data, dict)
        else None
    )
    if not isinstance(nodes, list):
        return []
    guides: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        path = str(node.get("path") or "")
        if not path:
            continue
        guides.append(
            {
                "title": str(node.get("title") or path),
                "url": urljoin(SPRING_SITE_BASE + "/", path),
                "slug": str(node.get("name") or path.strip("/").rsplit("/", 1)[-1]),
                "type": str(node.get("type") or ""),
                "category": [str(c) for c in (node.get("category") or [])],
                "description": str(node.get("description") or ""),
            }
        )
    return guides


def parse_sitemap_guides(text: str) -> list[dict]:
    """Parse a sitemap XML into guide entries (URLs only — no titles)."""
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
    guides: list[dict] = []
    for url in urls:
        if "/guides/" not in url:
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        guides.append(
            {
                "title": slug.replace("-", " ").title(),
                "url": url,
                "slug": slug,
                "type": _guide_kind(url),
                "category": [],
                "description": "",
            }
        )
    return guides


def _guide_kind(url: str) -> str:
    if "/guides/gs/" in url:
        return "getting-started"
    if "/guides/topicals/" in url:
        return "topical"
    if "/guides/tutorials/" in url:
        return "tutorial"
    return ""


def _build_guide_catalogue() -> dict:
    """Guide catalogue: page-data first, sitemap as the fallback."""
    fetched = _fetch_cached(SPRING_GUIDES_PAGE_DATA_URL, SPRING_PAGE_TTL)
    if fetched["ok"]:
        guides = parse_guides_page_data(fetched["text"])
        if guides:
            return {
                "ok": True,
                "source": "page-data",
                "source_url": fetched["url"],
                "guides": guides,
                "guide_count": len(guides),
                "built_at": _now_iso(),
            }

    index = _fetch_cached(SPRING_SITEMAP_INDEX_URL, SPRING_PAGE_TTL)
    if not index["ok"]:
        return _error(
            f"guide catalogue unavailable: {index['error']}",
            "spring_guides(guide='gs/spring-boot') still works for a known guide",
        )
    sitemaps = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", index["text"])
    for sitemap in sitemaps[:2]:
        body = _fetch_cached(sitemap, SPRING_PAGE_TTL)
        if not body["ok"]:
            continue
        guides = parse_sitemap_guides(body["text"])
        if guides:
            return {
                "ok": True,
                "source": "sitemap",
                "source_url": sitemap,
                "note": (
                    "spring.io's guide index is rendered client-side, so the "
                    "sitemap gives URLs only and titles are derived from the slug"
                ),
                "guides": guides,
                "guide_count": len(guides),
                "built_at": _now_iso(),
            }
    return _error(
        "no guide entries found on spring.io",
        "spring_guides(guide='gs/spring-boot') still works for a known guide",
    )


def load_guide_catalogue(*, force_refresh: bool = False) -> dict:
    """Cached guide catalogue.  Never raises."""
    if force_refresh:
        built = _build_guide_catalogue()
        if built.get("ok"):
            cache = _cache()
            if cache is not None:
                cache.set_value(GUIDES_INDEX_KEY, json.dumps(built, ensure_ascii=False), SPRING_PAGE_TTL)
        return built
    return _cached_json(GUIDES_INDEX_KEY, SPRING_PAGE_TTL, _build_guide_catalogue)


def guide_url_for(slug_or_url: str) -> str:
    """Normalise ``"gs/spring-boot"``, ``"spring-boot"`` or a full URL to a guide URL."""
    value = (slug_or_url or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    value = value.strip("/")
    if value.startswith("guides/"):
        value = value[len("guides/") :]
    if not value.startswith(("gs/", "topicals/", "tutorials/")):
        value = f"gs/{value}"
    return f"{SPRING_GUIDES_URL}/{value}/"


def parse_guide_html(html: str, url: str) -> dict:
    """Parse one spring.io guide page into clean markdown.

    Guide pages *are* server-rendered (measured: ``/guides/gs/spring-boot/``
    430 261 B with ``div.guide`` and the guide's ``<h1>``), unlike the guide
    index.  The breadcrumb block (``div.pb-5`` → "All guides") and the site
    chrome are dropped.  Raises ``ValueError`` when there is no guide container.
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.find("div", class_="guide") or soup.find("article") or soup.find("main")
    if container is None:
        raise ValueError(f"not a spring.io guide page (no guide container at {url})")

    title = ""
    if soup.title is not None:
        title = " ".join(soup.title.get_text(" ", strip=True).split())
    heading = container.find(["h1", "h2"])
    if not title and heading is not None:
        title = " ".join(heading.get_text(" ", strip=True).split())
    description = ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if meta is not None:
        description = " ".join((meta.get("content") or "").split())

    work = BeautifulSoup(str(container), "lxml")
    for selector in (
        "div.pb-5",
        "nav",
        "aside",
        "footer",
        "div.footer",
        "div.socials",
        "div.report-issue",
        "script",
        "style",
    ):
        for element in work.select(selector):
            element.decompose()

    body_md = _md(work, url)
    if not body_md:
        raise ValueError(f"guide page at {url} has no readable body")
    return {"title": title or url, "description": description, "markdown": body_md}


def fetch_guide_page(slug_or_url: str) -> dict:
    """Fetch one spring.io guide as markdown.  Never raises."""
    url = guide_url_for(slug_or_url)
    if urlparse(url).netloc != "spring.io" or "/guides/" not in url:
        return _error(
            f"{url!r} is not a spring.io guide URL",
            "use a slug like 'gs/accessing-data-jpa' or a full https://spring.io/guides/… URL",
        )
    fetched = _fetch_cached(url, SPRING_PAGE_TTL)
    if not fetched["ok"]:
        return fetched
    try:
        parsed = parse_guide_html(fetched["text"], fetched["url"])
    except ValueError as exc:
        return _error(str(exc), "spring_guides() (no arguments) lists the guides that exist")
    return {
        "ok": True,
        "url": fetched["url"],
        "cached": bool(fetched["cached"]),
        "title": parsed["title"],
        "description": parsed["description"],
        "markdown": parsed["markdown"],
    }


# ---------------------------------------------------------------------------
# Spring Initializr
# ---------------------------------------------------------------------------

def fetch_initializr_metadata() -> dict:
    """Fetch Spring Initializr's project metadata (v2.1 shape).  Never raises.

    ``https://start.spring.io/`` is the metadata endpoint; the ``Accept`` header
    pins the representation (measured: v2.1, HAL and ``*/*`` all return the same
    75 760 B body, only the content type differs).  robots.txt is 404 here, so
    the politeness layer allows the host.
    """
    return _fetch_cached(
        START_SPRING_BASE + "/", METADATA_TTL, headers={"Accept": INITIALIZR_ACCEPT}
    )


def _option_group(field: dict) -> dict:
    """Normalise one Initializr select field to ``{"default", "values"}``."""
    if not isinstance(field, dict):
        return {"default": None, "values": []}
    values = []
    for value in field.get("values") or []:
        if isinstance(value, dict):
            values.append({"id": str(value.get("id") or ""), "name": str(value.get("name") or "")})
    return {"default": field.get("default"), "values": values}


def _boot_sort_key(version_id: str):
    """Sort Initializr boot ids newest-first, keeping releases above snapshots.

    Initializr ids are ``4.1.1.RELEASE`` / ``4.1.2.BUILD-SNAPSHOT`` /
    ``4.2.0.M2``.  A snapshot of the *next* release must not outrank the newest
    *released* version, because "latest Spring Boot" means released.
    """
    text = str(version_id)
    rank = 0
    if "SNAPSHOT" in text.upper():
        rank = -1
    elif re.search(r"\.(M\d+|RC\d+|BUILD)$", text, re.I):
        rank = 0
    else:
        rank = 1
    numbers = [int(n) for n in re.findall(r"\d+", text)][:3]
    while len(numbers) < 3:
        numbers.append(0)
    return (rank, *numbers)


def _is_released(version_id: str) -> bool:
    text = str(version_id).upper()
    return not any(marker in text for marker in ("SNAPSHOT", ".M1", ".M2", ".M3", ".M4", ".RC"))


def _reference_link(links) -> str:
    """The documentation URL of an Initializr dependency, templated placeholders filled.

    HAL is loose here: ``_links.reference`` is an object for most dependencies and
    a **list** for the ones that document several things (measured: ``rest-websockets``
    returns a list).  ``href`` is also often ``templated`` — it contains
    ``{bootVersion}``, which is not a URL anyone can open.
    """
    if not isinstance(links, dict):
        return ""
    targets = links.get("reference") or []
    if isinstance(targets, dict):
        targets = [targets]
    if not isinstance(targets, list):
        return ""
    for target in targets:
        if isinstance(target, dict) and target.get("href"):
            return str(target["href"])
    return ""


def _de_template(url: str, boot_version: str | None) -> str:
    """Replace Initializr's ``{bootVersion}`` placeholder with a real version."""
    if not url or "{bootVersion}" not in url:
        return url
    return url.replace("{bootVersion}", boot_version or "current")


def parse_initializr_metadata(data) -> dict:
    """Parse Initializr metadata into options + a flat dependency catalogue.

    Pure function; returns ``{}`` for anything that is not the metadata object.
    """
    if not isinstance(data, dict) or "bootVersion" not in data:
        return {}
    boot = _option_group(data.get("bootVersion"))
    boot_ids = [v["id"] for v in boot["values"] if v["id"]]
    released = [v for v in boot_ids if _is_released(v)]
    groups = data.get("dependencies") or {}
    newest_plain = plain_boot_version(
        max((v for v in boot_ids if _is_released(v)), key=_boot_sort_key, default=None)
    )
    catalogue: list[dict] = []
    group_names: list[dict] = []
    if isinstance(groups, dict):
        items = [(str(g.get("name") or ""), g.get("values") or []) for g in groups.get("values") or [] if isinstance(g, dict)]
    elif isinstance(groups, list):
        items = [(str(g.get("name") or ""), g.get("values") or []) for g in groups if isinstance(g, dict)]
    else:
        items = []
    for group_name, values in items:
        group_names.append({"name": group_name, "count": len(values)})
        for value in values:
            if not isinstance(value, dict):
                continue
            reference = _reference_link(value.get("_links"))
            catalogue.append(
                {
                    "id": str(value.get("id") or ""),
                    "name": str(value.get("name") or ""),
                    "description": " ".join((value.get("description") or "").split()),
                    "group": group_name,
                    "reference_url": _de_template(reference, newest_plain),
                }
            )
    return {
        "boot_versions": {
            "default": boot.get("default"),
            "latest_released": max(released, key=_boot_sort_key) if released else None,
            "values": boot["values"],
        },
        "java_versions": _option_group(data.get("javaVersion")),
        "languages": _option_group(data.get("language")),
        "types": _option_group(data.get("type")),
        "packagings": _option_group(data.get("packaging")),
        "defaults": {
            key: (data.get(key) or {}).get("default")
            for key in ("groupId", "artifactId", "version", "name", "description", "packageName")
            if isinstance(data.get(key), dict)
        },
        "dependency_groups": group_names,
        "dependencies": catalogue,
        "dependency_count": len(catalogue),
    }


def fetch_initializr_build_files(ids: list[str], boot_version: str | None = None) -> dict:
    """Ask Initializr to generate the real Maven and Gradle dependency blocks.

    This is the anti-invention mechanism: the coordinates come from the build
    file Initializr itself would put in your project.  Measured for
    ``dependencies=data-jpa,web`` on 4.1.1: ``spring-boot-starter-data-jpa`` and
    ``spring-boot-starter-webmvc`` — the ``web`` id does **not** map to
    ``spring-boot-starter-web`` in Spring Boot 4.1.

    ``boot_version`` must be the plain version (``4.1.1``); the ``.RELEASE``
    form makes ``/build.gradle`` fail with HTTP 500 (measured).
    """
    joined = ",".join(i for i in ids if i)
    if not joined:
        return _error("no dependency ids given", "pass ids from the Initializr catalogue")
    params = {"type": "maven-build", "dependencies": joined}
    gradle_params = {"type": "gradle-build", "dependencies": joined}
    if boot_version:
        cleaned = str(boot_version).strip().lstrip("vV")
        if not _VERSION_RE.match(cleaned):
            return _error(
                f"boot_version {boot_version!r} must look like '4.1' or '4.1.1'",
                "use the plain version: Initializr rejects '4.1.1.RELEASE' on /build.gradle",
            )
        params["bootVersion"] = cleaned
        gradle_params["bootVersion"] = cleaned

    pom_url = f"{START_SPRING_BASE}/pom.xml?{urlencode(params)}"
    gradle_url = f"{START_SPRING_BASE}/build.gradle?{urlencode(gradle_params)}"

    pom = _fetch_cached(pom_url, METADATA_TTL)
    gradle = _fetch_cached(gradle_url, METADATA_TTL)
    if not pom["ok"] and not gradle["ok"]:
        return pom if not pom["ok"] else gradle

    return {
        "ok": True,
        "pom_url": pom_url,
        "gradle_url": gradle_url,
        "pom": pom,
        "gradle": gradle,
        "coordinates": parse_pom_dependencies(pom["text"]) if pom["ok"] else [],
        "gradle_coordinates": parse_gradle_dependencies(gradle["text"]) if gradle["ok"] else [],
        "cached": bool(pom.get("cached")) and bool(gradle.get("cached")),
    }


_POM_DEPENDENCY_RE = re.compile(
    r"<dependency>\s*"
    r"<groupId>([^<]+)</groupId>\s*"
    r"<artifactId>([^<]+)</artifactId>(.*?)</dependency>",
    re.S,
)


def parse_pom_dependencies(xml: str) -> list[dict]:
    """Parse ``<dependency>`` blocks out of an Initializr-generated pom.xml."""
    out: list[dict] = []
    for group_id, artifact_id, tail in _POM_DEPENDENCY_RE.findall(xml or ""):
        scope = re.search(r"<scope>([^<]+)</scope>", tail)
        out.append(
            {
                "group_id": group_id.strip(),
                "artifact_id": artifact_id.strip(),
                "scope": scope.group(1).strip() if scope else "compile",
            }
        )
    return out


_GRADLE_DEPENDENCY_RE = re.compile(
    r"^\s*(implementation|testImplementation|runtimeOnly|testRuntimeOnly|annotationProcessor|api|compileOnly)\s+['\"]([^'\"]+)['\"]",
    re.M,
)


def parse_gradle_dependencies(text: str) -> list[dict]:
    """Parse ``implementation 'g:a'`` lines out of an Initializr-generated build.gradle."""
    out: list[dict] = []
    for configuration, coordinate in _GRADLE_DEPENDENCY_RE.findall(text or ""):
        group_id, _, artifact_id = coordinate.partition(":")
        out.append(
            {
                "group_id": group_id.strip(),
                "artifact_id": artifact_id.strip(),
                "scope": "test" if configuration.startswith("test") else "compile",
                "configuration": configuration,
            }
        )
    return out


def plain_boot_version(version_id) -> str | None:
    """Initializr's build-file endpoints want the plain version: ``4.1.1``, not ``4.1.1.RELEASE``.

    Measured: ``/build.gradle?type=gradle-build&bootVersion=4.1.1.RELEASE`` answers
    ``500 {"message":"… Bom 'org.springframework.boot:spring-boot-dependencies:
    4.1.1.RELEASE' could not be resolved"}`` while ``bootVersion=4.1.1`` answers
    ``200``.  A suffix we cannot strip (``4.2.0.M2``) returns ``None`` rather than
    a plausible-looking "4.2.0" that is not a released line.
    """
    text = str(version_id or "").strip().lstrip("vV")
    if not text:
        return None
    candidate = re.sub(r"[._-](?:RELEASE|release)$", "", text)
    return candidate if _VERSION_RE.match(candidate) else None


def maven_snippet(coordinates: list[dict]) -> str:
    """Ready-to-paste ``<dependency>`` block for the coordinates Initializr emitted."""
    if not coordinates:
        return ""
    lines = ["<dependencies>"]
    for item in coordinates:
        lines.append("    <dependency>")
        lines.append(f"        <groupId>{item['group_id']}</groupId>")
        lines.append(f"        <artifactId>{item['artifact_id']}</artifactId>")
        if item.get("scope") and item["scope"] != "compile":
            lines.append(f"        <scope>{item['scope']}</scope>")
        lines.append("    </dependency>")
    lines.append("</dependencies>")
    return "\n".join(lines)


def gradle_snippet(coordinates: list[dict]) -> str:
    """Ready-to-paste ``dependencies { … }`` block for those coordinates."""
    if not coordinates:
        return ""
    lines = ["dependencies {"]
    for item in coordinates:
        lines.append(
            f"    {item.get('configuration') or ('testImplementation' if item['scope'] == 'test' else 'implementation')} "
            f"'{item['group_id']}:{item['artifact_id']}'"
        )
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def fetch_github_releases(project: str = "spring-boot", per_page: int = 100) -> dict:
    """Fetch a project's GitHub releases (cached a day — 60 requests/hour/IP).

    ``per_page=100`` is deliberate: one request returns the whole recent
    history (measured: 100 releases, 1.1 MB, 76 stable), so resolving a version
    and reading its notes costs **one** request instead of a search plus a
    lookup.
    """
    repo = GITHUB_REPOS.get((project or "").strip().lower())
    if repo is None:
        return _error(
            f"unknown project {project!r}",
            "supported: " + ", ".join(sorted(GITHUB_REPOS)),
        )
    limit = max(1, min(int(per_page), 100))
    url = f"{GITHUB_API_BASE}/repos/{repo}/releases?per_page={limit}"
    fetched = _fetch_cached(url, METADATA_TTL, headers={"Accept": GITHUB_ACCEPT})
    if not fetched["ok"]:
        return fetched
    try:
        releases = json.loads(fetched["text"])
    except (TypeError, ValueError):
        return _error(
            f"GitHub returned a non-JSON body for {url}",
            "GitHub may be rate-limiting this IP (60/hour unauthenticated); try again later",
        )
    if not isinstance(releases, list):
        message = releases.get("message") if isinstance(releases, dict) else None
        return _error(
            f"GitHub returned {message or 'an unexpected body'} for {repo}",
            "unauthenticated GitHub is limited to 60 requests per hour per IP; retry later",
        )
    return {
        "ok": True,
        "url": url,
        "repo": repo,
        "releases": releases,
        "release_count": len(releases),
        "cached": bool(fetched["cached"]),
    }


#: Measured over 100 real spring-boot release bodies (see the module docstring).
#: Emoji prefixes vary, so the match is on the heading text after stripping them.
_RELEASE_SECTION_KINDS = {
    "new features": "new-features",
    "attention required": "breaking-changes",
    "noteworthy changes": "breaking-changes",
    "noteworthy": "breaking-changes",
    "breaking changes": "breaking-changes",
    "bug fixes": "bug-fixes",
    "dependency upgrades": "dependency-upgrades",
    "documentation": "documentation",
    "contributors": "contributors",
}

#: Keywords that mark a bullet as a deprecation or removal, whatever section it
#: sits in (no spring-boot release body has a "Deprecations" heading).
_DEPRECATION_RE = re.compile(
    r"\bdeprecat\w*\b|\bno longer\b|\bremoved\b|\bdrop(?:ped|s)?\b|\bend(?:ed|s)? of support\b",
    re.I,
)


def _strip_emoji(heading: str) -> str:
    """Lower-case a GitHub release heading, dropping emoji in both spellings.

    GitHub release bodies spell their section icons as shortcodes — ``## :warning:
    Attention Required`` — so stripping only non-word characters leaves the word
    "warning" glued to the heading and the kind lookup misses (measured: every
    section of v4.1.1 came back as ``other``).  Literal glyphs are stripped too.
    """
    text = re.sub(r":[A-Za-z0-9_+-]+:", " ", str(heading))
    text = re.sub(r"[^\w\s&/-]", " ", text)
    return " ".join(text.split()).lower()


def parse_release(release: dict) -> dict:
    """Split one GitHub release body into its ``##`` sections.

    Returns ``{"tag", "name", "published_at", "url", "prerelease", "sections":
    [{"heading", "kind", "bullets"}], "wiki_links": [...]}``.
    """
    body = str(release.get("body") or "")
    sections: list[dict] = []
    current: dict | None = None
    for line in body.splitlines():
        heading = re.match(r"^##+\s+(.*)$", line)
        if heading:
            raw = heading.group(1).strip()
            kind = _RELEASE_SECTION_KINDS.get(_strip_emoji(raw), "other")
            current = {"heading": raw, "kind": kind, "bullets": []}
            sections.append(current)
            continue
        if current is None:
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet:
            text = " ".join(bullet.group(1).split())
            if text and _strip_emoji(text) != "contributors":
                current["bullets"].append(text)
    for section in sections:
        section["bullets"] = [b for b in section["bullets"] if b]
    return {
        "tag": release.get("tag_name") or "",
        "name": release.get("name") or "",
        "published_at": release.get("published_at") or "",
        "url": release.get("html_url") or "",
        "prerelease": bool(release.get("prerelease")),
        "sections": sections,
        # Only Spring's own wiki pages count as migration notes: a v4.1.1 body
        # also links the Jackson and other dependency wikis, which are upgrade
        # reading, not Spring migration notes.
        "wiki_links": [
            link
            for link in re.findall(r"https://github\.com/[^)\s]+/wiki/[^\s)\]]+", body)
            if "/spring-projects/" in link
        ],
    }


def filter_release_sections(sections: list[dict], focus: str = "all") -> dict:
    """Group a release's bullets by focus, from the measured heading kinds.

    ``breaking-changes`` = the ``Attention Required`` / ``Noteworthy`` sections.
    ``deprecations`` = keyword-matched bullets across every section, because
    spring-boot release bodies have no deprecation heading.
    """
    focus = (focus or "all").strip().lower()
    if focus not in {"all", "breaking-changes", "new-features", "deprecations"}:
        focus = "all"
    grouped = {
        "new-features": [],
        "breaking-changes": [],
        "deprecations": [],
        "bug-fixes": [],
        "dependency-upgrades": [],
        "documentation": [],
        "other": [],
    }
    for section in sections:
        for bullet in section["bullets"]:
            grouped.setdefault(section["kind"], []).append(bullet)
            if _DEPRECATION_RE.search(bullet) and section["kind"] not in ("contributors",):
                grouped["deprecations"].append(bullet)
    if focus == "all":
        return grouped
    if focus == "deprecations":
        return {"deprecations": grouped["deprecations"]}
    if focus == "new-features":
        return {"new-features": grouped["new-features"]}
    return {"breaking-changes": grouped["breaking-changes"]}


def normalize_version_tag(version: str) -> str:
    """``"4.1.1"`` / ``"v4.1.1"`` / ``"4.1"`` → the tag prefix to look for."""
    return str(version or "").strip().lstrip("vV")


# ---------------------------------------------------------------------------
# Ranking (offline)
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def _token_score(token: str, text: str) -> float:
    """Exact 4 / word-prefix 3 / substring 2 / no match 0 — same ladder as
    :func:`java_spring_mcp.search._token_score`, so Spring hits and JDK hits
    rank alike."""
    if not text:
        return 0.0
    if token == text:
        return 4.0
    words = text.split()
    if any(token == word for word in words):
        return 3.0
    if any(word.startswith(token) for word in words) or text.startswith(token):
        return 2.0
    if token in text:
        return 1.0
    return 0.0


#: Which fields a concept query may match, and how much each is worth.
#:
#: ``section`` is worth nearly as much as a title because the best answer to
#: "actuator" is ``reference/actuator/index.html`` — titled "Production-ready
#: Features", reachable only through its section.  Measured with the first 0.6
#: weight, ``how-to/actuator.html`` (title literally "Actuator", no reference
#: bonus) outranked the section page; at 0.9 the section page wins and its 13
#: subpages follow it.
_ENTRY_FIELDS = (
    ("title", 1.0),
    ("slug", 0.75),
    ("section", 0.9),
    ("parent", 0.35),
)

#: Bonus for pages that are part of the reference proper rather than the
#: appendix / API listings: a concept answer should be a prose page.
_REFERENCE_BONUS = 1.15


def rank_entries(
    query: str,
    entries: list[dict],
    limit: int | None = None,
    *,
    require_all: bool = True,
) -> list[dict]:
    """Rank nav entries against ``query``.

    With ``require_all`` (the default) every token must match some field — the
    same AND semantics as :func:`java_spring_mcp.search.search`.  With it off,
    partially matching entries survive as weak hits, halved in score so they can
    never outrank a real one: used only as a last resort, so an obscure query
    returns *something ranked* rather than nothing.

    Each hit carries ``matched_tokens``, ``matched_fields``, ``is_reference``.
    Ties break on shallower nav depth first, so "actuator" answers with the
    section page and not one of its 17 subpages.
    """
    tokens = _normalize(query).split()
    if not tokens:
        return []
    results: list[dict] = []
    for entry in entries:
        fields = {
            "title": _normalize(entry.get("title")),
            "slug": _normalize(str(entry.get("slug", "")).replace("/", " ").replace("-", " ")),
            "section": _normalize(entry.get("section")),
            "parent": _normalize(entry.get("parent")),
        }
        score = 0.0
        matched_fields: set[str] = set()
        matched_tokens = 0
        for token in tokens:
            best = 0.0
            best_field = ""
            for field, weight in _ENTRY_FIELDS:
                value = _token_score(token, fields[field]) * weight
                if value > best:
                    best = value
                    best_field = field
            if best <= 0.0:
                break
            score += best
            matched_tokens += 1
            matched_fields.add(best_field)
        if matched_tokens < len(tokens):
            if require_all:
                continue
            score *= 0.5  # a partial hit is a hint, not an answer
        if score <= 0.0:
            continue
        is_reference = _is_reference_path(entry.get("path", ""))
        if is_reference:
            score *= _REFERENCE_BONUS
        item = dict(entry)
        item["score"] = round(score, 3)
        item["matched_tokens"] = matched_tokens
        item["matched_fields"] = sorted(matched_fields)
        item["is_reference"] = is_reference
        item["matched_on"] = "title" if "title" in matched_fields else "path"
        results.append(item)
    results.sort(key=lambda r: (-r["score"], r["depth"], r["title"].lower()))
    return results[:limit] if limit else results


def rank_headings(
    query: str, headings: list[dict], page: dict, limit: int | None = None
) -> list[dict]:
    """Rank a page's in-page headings against ``query`` (same AND semantics)."""
    tokens = _normalize(query).split()
    if not tokens:
        return []
    page_title = _normalize(page.get("title"))
    results: list[dict] = []
    for heading in headings:
        fields = (
            (_normalize(heading.get("text")), 1.0),
            (_normalize(str(heading.get("anchor", "")).replace(".", " ").replace("-", " ")), 0.6),
            (page_title, 0.5),
        )
        score = 0.0
        ok = True
        for token in tokens:
            best = max(_token_score(token, text) * weight for text, weight in fields)
            if best <= 0.0:
                ok = False
                break
            score += best
        if not ok or score <= 0.0:
            continue
        item = dict(heading)
        item["score"] = round(score * _REFERENCE_BONUS, 3)
        item["page_title"] = page.get("title") or ""
        item["page_url"] = page.get("url") or ""
        results.append(item)
    results.sort(key=lambda r: (-r["score"], r["text"].lower()))
    return results[:limit] if limit else results


def candidate_pages(query: str, entries: list[dict], limit: int = HEADING_PAGE_LIMIT) -> list[dict]:
    """Reference pages worth fetching to read their in-page headings.

    A page is a candidate when **some but not all** tokens match its title, slug
    or section: "data jpa" matches ``data/sql.html`` through the section
    ``data``, and the leftover token ``jpa`` is exactly what only an in-page
    heading can answer (measured heading: "JPA and Spring Data JPA").  Pages that
    already match the whole query are ranked by :func:`rank_entries` and need no
    expansion, so they are ranked *below* the partial ones here — the budget is
    spent where the answer is still unknown.
    """
    tokens = _normalize(query).split()
    if not tokens:
        return []
    candidates: list[dict] = []
    for entry in entries:
        path = str(entry.get("path", ""))
        if not _is_reference_path(path) or not path.endswith(".html"):
            continue
        fields = (
            (_normalize(entry.get("title")), 1.0),
            (_normalize(str(entry.get("slug", "")).replace("/", " ").replace("-", " ")), 0.75),
            (_normalize(entry.get("section")), 0.6),
        )
        score = 0.0
        matched = 0
        for token in tokens:
            best = max(_token_score(token, text) * weight for text, weight in fields)
            if best > 0.0:
                matched += 1
                score += best
        if matched == 0:
            continue
        item = dict(entry)
        item["score"] = round(score, 3)
        item["matched_tokens"] = matched
        item["partial"] = matched < len(tokens)
        # Shallowest first among equals: a section index page is a better place
        # to look for a topic than one of its subpages.
        item["path_depth"] = path.count("/")
        candidates.append(item)
    candidates.sort(key=lambda c: (not c["partial"], -c["score"], c["path_depth"]))
    return candidates[:limit] if limit else candidates


#: Which fields of an Initializr dependency a need may match, and their weights.
_DEPENDENCY_FIELDS = (
    ("id", 1.0),
    ("name", 1.0),
    ("description", 0.6),
    ("group", 0.4),
)


def rank_dependencies(
    query: str, dependencies: list[dict], limit: int | None = None
) -> list[dict]:
    """Rank Initializr dependencies against a need like ``"jpa"``.

    Every token must match some field of the catalogue entry — a dependency that
    does not match is **not** returned.  That is the point: this tool may only
    recommend artifacts the Initializr catalogue actually offers, so an unmatched
    need yields no candidates rather than a plausible-sounding starter.
    """
    tokens = _normalize(query).split()
    if not tokens:
        return []
    results: list[dict] = []
    for dep in dependencies:
        fields = {
            "id": _normalize(str(dep.get("id", "")).replace("-", " ")),
            "name": _normalize(dep.get("name")),
            "description": _normalize(dep.get("description")),
            "group": _normalize(dep.get("group")),
        }
        score = 0.0
        matched_fields: set[str] = set()
        for token in tokens:
            best = 0.0
            best_field = ""
            for field, weight in _DEPENDENCY_FIELDS:
                value = _token_score(token, fields[field]) * weight
                if value > best:
                    best = value
                    best_field = field
            if best <= 0.0:
                score = 0.0
                break
            score += best
            matched_fields.add(best_field)
        if score <= 0.0:
            continue
        item = dict(dep)
        item["score"] = round(score, 3)
        item["matched_fields"] = sorted(matched_fields)
        results.append(item)
    results.sort(key=lambda r: (-r["score"], r["id"]))
    return results[:limit] if limit else results


# ---------------------------------------------------------------------------
# Reference docs: resolving a section/subsection to a URL
# ---------------------------------------------------------------------------

def _dir_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _dir_name(path: str) -> str:
    return _dir_of(path).rsplit("/", 1)[-1]


def _is_reference_path(path: str) -> bool:
    """True for prose pages of the reference proper (not api/ or appendix/)."""
    return str(path).startswith("/spring-boot/reference/")


def _section_landing_pages(entries: list[dict]) -> dict[str, str]:
    """``{section_name: url of its index page}`` — the sections a caller can ask for."""
    pages: dict[str, str] = {}
    for entry in entries:
        if not _is_reference_path(entry["path"]):
            continue
        if entry["slug"].lower() == "index" and entry["section"] not in pages:
            pages[entry["section"]] = entry["url"]
    return pages


def resolve_reference_url(
    section: str, subsection: str | None = None, version: str | None = None
) -> dict:
    """Resolve ``(section, subsection)`` to a docs URL using the concept index.

    Offline when the index is cached, which is the normal case: the answer is a
    lookup over 296 known nav paths instead of guessing URLs and eating request
    budget on 404s.  It also fixes the names people actually use — "how-to" is a
    directory, not a page, and the Web section's own page is ``web/index.html``
    titled "Web", not ``web.html``.

    Returns ``{"ok", "url", "title", "path", "section"}``, or an error dict whose
    ``candidates`` lists nav titles that do contain the word.
    """
    sec = str(section or "").strip().strip("/").lower()
    if not sec:
        return _error(
            "section is required",
            "e.g. spring_reference('web'), spring_reference('data', 'sql') or spring_reference('how-to', 'actuator')",
        )
    sec = re.sub(r"\.html$", "", sec)
    sub = re.sub(r"\.html$", "", str(subsection or "").strip().strip("/").lower())

    index = load_concept_index(version)
    if not index.get("ok"):
        return index
    entries = index.get("entries") or []

    def matches(entry: dict, needle: str) -> bool:
        """Match a page or directory name, hyphens and spaces being the same thing.

        People write "graceful shutdown" and "auto configuration"; the pages are
        ``graceful-shutdown.html`` and ``auto-configuration.html``.  Comparing the
        normalized forms is what makes ``spring_reference("web", "graceful shutdown")``
        resolve instead of falling back to the section page.
        """
        return (
            entry["slug"].lower() == needle
            or _dir_name(entry["path"]).lower() == needle
            or _normalize(entry["slug"]) == _normalize(needle)
        )

    def order(entry: dict):
        """Reference pages first, then the section's own page, then shallower.

        Without the reference-first key, ``spring_reference("actuator")`` picks
        ``api/rest/actuator/index.html`` — the javadoc listing — because it is
        also a directory called "actuator".  Measured on the 4.1.1 nav.
        """
        return (
            not _is_reference_path(entry["path"]),
            entry["slug"].lower() != "index",
            entry["depth"],
            entry["path"],
        )

    section_hits = [e for e in entries if matches(e, sec)]
    matched_globally = False
    if sub:
        dirs = {_dir_of(e["path"]).lower() for e in section_hits}
        scoped = [e for e in entries if matches(e, sub) and _dir_of(e["path"]).lower() in dirs]
        if scoped:
            chosen = sorted(scoped, key=order)
        else:
            # The subsection exists, just not under that section.  Answer with
            # it and say so — a caller who wrote ("web", "profiles") has read
            # those words somewhere and needs the page, not a lecture.
            chosen = sorted((e for e in entries if matches(e, sub)), key=order)
            matched_globally = bool(chosen)
    else:
        chosen = sorted(section_hits, key=order)

    if not chosen:
        near = rank_entries(sec, entries, 8, require_all=False)
        candidates = [{"title": h["title"], "url": h["url"]} for h in near[:8]]
        if not candidates:
            # Nothing even resembles the name: list the sections that do exist.
            candidates = [
                {"title": title, "url": url}
                for title, url in sorted(_section_landing_pages(entries).items())
            ][:12]
        return _error(
            f"no Spring Boot documentation section named {section!r}"
            + (f" with subsection {subsection!r}" if subsection else ""),
            "spring_search_concepts('<topic>') lists the sections that exist",
            extra={"candidates": candidates},
        )
    best = chosen[0]
    result = {
        "ok": True,
        "url": best["url"],
        "path": best["path"],
        "title": best["title"],
        "section": best["section"],
        "alternatives": [
            {"title": e["title"], "url": e["url"]} for e in chosen[1:5]
        ],
        "index_version": index.get("version") or "",
    }
    if matched_globally:
        result["note"] = (
            f"{subsection!r} is not a page under {section!r}; it lives at "
            f"{best['path']} (matched by page name)"
        )
    return result


def fetch_reference_url(url: str) -> dict:
    """Fetch a **known** docs URL and parse it as a Spring Boot reference page.

    Used once :func:`resolve_reference_url` has picked the URL, so no request is
    ever spent guessing one.  Same parser as
    :func:`java_spring_mcp.fetchers.fetch_spring_boot_section` uses.
    """
    parsed = urlparse(url)
    if parsed.netloc != "docs.spring.io":
        return _error(
            f"{url!r} is not a docs.spring.io URL",
            "pass a section name instead, e.g. spring_reference('web')",
        )
    raw = _fetch_cached(url, SPRING_PAGE_TTL)
    if not raw["ok"]:
        return raw
    try:
        document = parse_spring_section_html(raw["text"], raw["url"])
    except ValueError as exc:
        return _error(str(exc), "spring_search_concepts('<topic>') lists the pages that exist")
    headings: list[dict] = []
    try:
        headings = parse_page_headings(raw["text"], raw["url"])
    except ValueError:
        headings = []
    return {
        "ok": True,
        "url": raw["url"],
        "cached": bool(raw["cached"]),
        "title": document.get("title") or "",
        "markdown": document.get("markdown") or "",
        "headings": headings,
    }
