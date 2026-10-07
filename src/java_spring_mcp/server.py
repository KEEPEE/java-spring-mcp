"""MCP server exposing JDK / Spring Boot / Maven Central documentation tools.

Tool set:

- ``health_check()`` — trivial liveness probe (server name + version).
- ``java_docs(identifier, topic=None, max_tokens=8000)`` — fetch a JDK class
  javadoc page or a Spring Boot reference section and return clean markdown.
  Identifier resolution order:

  1. ``"spring"`` / ``"spring:<section>"`` → Spring Boot reference section;
  2. dotted names (e.g. ``java.util.List``) → fully-qualified class, module
     resolved from the search index when possible, else ``java.base`` plus a
     fixed fallback-module retry list;
  3. plain names (e.g. ``HttpClient``) → exact-name lookup in the search
     index (class/interface preferred on ambiguity), then fetch.

  Optional ``topic`` keeps only the matching heading section (plus title and
  description); ``max_tokens`` truncates the final markdown to a token budget
  (estimated as ``len(text) // 4``) cut at a line boundary.
- ``java_search(query, limit=8)`` — rank JDK classes and Spring Boot
  reference sections by name from the cached search index.
- ``maven_package(group_id=None, artifact_id, version=None, max_tokens=6000)``
  — Maven Central metadata (newest matching version, top 20 versions, links).
  ``max_tokens`` is reserved for future javadoc content fetching.
- ``java_status()`` — real health check over the search index, the local
  cache and light GET probes of the three upstream endpoints.

Design rules (hard requirements):

- No tool ever calls or delegates to another tool; each one does its own work
  via :mod:`java_spring_mcp.fetchers`, :mod:`java_spring_mcp.search` and
  :class:`~java_spring_mcp.cache.DocCache`.
- Every tool returns a plain dict. On ANY failure the result is
  ``{"error": "<short message>", "suggestion": "<what to try instead>"}``;
  no exception ever escapes a tool and nothing recurses.
- Fetched content is cached in :class:`~java_spring_mcp.cache.DocCache` keyed
  by source URL: JDK docs TTL 7 days, Spring sections TTL 7 days, Maven
  metadata TTL 1 day. Failures are never cached.
- Politeness: every request the tools make goes through
  :mod:`java_spring_mcp.politeness` (robots.txt, per-host throttle, retry /
  ``Retry-After``, conditional GET) and spends one request budget per tool call
  (:func:`java_spring_mcp.fetchers.tool_budget`, cap
  :data:`~java_spring_mcp.fetchers.FETCH_BUDGET_LIMIT`). ``java_status``
  reports the layer's counters under a top-level ``politeness`` key — outside
  ``checks``, so diagnostics can never make ``overall`` worse.
"""

from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import urlencode

import httpx
from mcp.server.fastmcp import FastMCP

from . import __version__
from .cache import DocCache
from .fetchers import (
    FETCH_BUDGET_LIMIT,
    JDK_API_BASE,
    MAVEN_SEARCH_URL,
    SPRING_REFERENCE_BASE,
    fetch_jdk_class,
    fetch_maven_artifact,
    fetch_spring_boot_section,
    get_politeness,
    tool_budget,
)
from . import spring
from .politeness import default_robots_db_path
from .search import load_index, search

mcp = FastMCP("java-spring-mcp")

#: Cache TTLs (seconds). JDK docs and Spring sections: one week; Maven
#: metadata: one day.
JDK_DOC_TTL = 604800
SPRING_DOC_TTL = 604800
MAVEN_META_TTL = 86400

#: Modules tried (in order) after the default ``java.base`` fails for a
#: fully-qualified name whose module could not be resolved from the index.
_FALLBACK_MODULES = (
    "jdk.httpclient",
    "java.sql",
    "java.desktop",
    "jdk.crypto.ec",
    "jdk.management",
)

#: Light GET probes for java_status (name, url).
_ENDPOINT_PROBES = (
    ("docs_oracle_com", f"{JDK_API_BASE}/java.base/java/util/List.html"),
    ("docs_spring_io", f"{SPRING_REFERENCE_BASE}/index.html"),
    ("search_maven_org", f"{MAVEN_SEARCH_URL}?q=guava&rows=1&wt=json"),
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# ---------------------------------------------------------------------------
# Small shared helpers (not exposed as tools)
# ---------------------------------------------------------------------------

def _cache() -> DocCache | None:
    """A fresh DocCache, or ``None`` when the cache is unavailable."""
    try:
        return DocCache()
    except Exception:
        return None


def _cache_get(key: str) -> dict | None:
    """Return the cached doc dict for ``key`` (fresh, valid JSON), else None."""
    cache = _cache()
    if cache is None:
        return None
    try:
        raw = cache.get(key)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("markdown"), str):
        return data
    return None


def _cache_set(key: str, value: dict, ttl_seconds: int) -> None:
    """Store a doc dict under ``key``; cache-write failures are ignored.

    ``set_value`` (not ``set``) on purpose: the fetcher keeps the raw body and
    the ``etag`` / ``last_modified`` of that same URL in the same row, and
    ``set`` would clear them to NULL — the conditional GET would silently stop
    happening while everything still looked fine.
    """
    cache = _cache()
    if cache is None:
        return
    try:
        cache.set_value(key, json.dumps(value), ttl_seconds)
    except Exception:
        pass


def _spring_section_url(section: str) -> str:
    """Source URL for a Spring Boot reference section (mirrors the fetcher)."""
    sec = (section or "index").strip().strip("/").lower() or "index"
    if sec == "index":
        return f"{SPRING_REFERENCE_BASE}/index.html"
    return f"{SPRING_REFERENCE_BASE}/{sec}/index.html"


def _jdk_class_url(fully_qualified: str, module: str) -> str:
    """Source URL for a JDK javadoc class page (mirrors the fetcher)."""
    package, _, simple = fully_qualified.rpartition(".")
    return f"{JDK_API_BASE}/{module}/{package.replace('.', '/')}/{simple}.html"


def _maven_search_url(group_id: str | None, artifact_id: str) -> str:
    """The exact search-API URL the fetcher will use (cache key)."""
    group = (group_id or "").strip() or None
    artifact = (artifact_id or "").strip()
    query = f'g:"{group}" AND a:"{artifact}"' if group else f'a:"{artifact}"'
    params = {"q": query, "core": "gav", "rows": 20, "wt": "json"}
    return f"{MAVEN_SEARCH_URL}?{urlencode(params)}"


def _module_from_index(fully_qualified: str, index: dict | None) -> str | None:
    """Module for an exact FQN (last-segment name + package) in the index."""
    entries = (index or {}).get("entries") or []
    package, _, simple = fully_qualified.rpartition(".")
    for entry in entries:
        if entry.get("name") == simple and entry.get("package") == package:
            return entry.get("module")
    return None


def _extract_topic_section(markdown: str, topic: str) -> tuple[str | None, list[str]]:
    """Extract the first heading section whose text contains ``topic``.

    Returns ``(section_markdown_or_None, all_heading_texts)``.  The section
    runs from the matched heading up to (excluding) the next heading of the
    same or a higher level.
    """
    lines = markdown.splitlines()
    headings: list[str] = []
    target: tuple[int, int] | None = None  # (line index, heading level)
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if not match:
            continue
        text = match.group(2).strip()
        headings.append(text)
        if target is None and topic.lower() in text.lower():
            target = (i, len(match.group(1)))
    if target is None:
        return None, headings
    idx, level = target
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        match = _HEADING_RE.match(lines[j].strip())
        if match and len(match.group(1)) <= level:
            end = j
            break
    return "\n".join(lines[idx:end]).rstrip(), headings


def _title_and_description(markdown: str) -> str:
    """Everything before the first level-2 heading (title + description)."""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 2:
            return "\n".join(lines[:i]).strip()
    # No level-2 heading: keep only the title line to avoid duplicating body.
    return lines[0].strip() if lines else ""


def _apply_topic(markdown: str, topic: str | None) -> tuple[str, str | None]:
    """Apply optional topic filtering; returns (content, note_or_None)."""
    if not topic:
        return markdown, None
    section, headings = _extract_topic_section(markdown, str(topic))
    if section is None:
        listing = ", ".join(headings[:25]) or "(none)"
        return markdown, (
            f"no section matching {topic!r}; available headings: {listing}"
        )
    preface = _title_and_description(markdown)
    content = f"{preface}\n\n{section}".strip() if preface else section
    return content, None


def _truncate_markdown(markdown: str, max_tokens: int | None) -> tuple[str, bool]:
    """Truncate to ~max_tokens tokens (len//4 estimate) at a line boundary."""
    if not max_tokens or max_tokens <= 0:
        return markdown, False
    total = len(markdown) // 4
    if total <= int(max_tokens):
        return markdown, False
    budget = int(max_tokens) * 4
    cut = markdown[:budget]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    shown = len(cut) // 4
    note = f"\n\n[truncated: showing ~{shown} of ~{total} estimated tokens]"
    return cut.rstrip() + note, True


def _doc_payload(
    *,
    doc_type: str,
    identifier: str,
    url: str,
    title: str | None,
    markdown: str,
    topic: str | None,
    max_tokens: int,
    cached: bool,
    note: str | None = None,
) -> dict:
    """Assemble the java_docs success shape (topic filter + truncation)."""
    content, topic_note = _apply_topic(markdown, topic)
    if topic_note and note:
        note = f"{note}; {topic_note}"
    elif topic_note:
        note = topic_note
    content, truncated = _truncate_markdown(content, max_tokens)
    payload: dict = {
        "type": doc_type,
        "identifier": identifier,
        "url": url,
        "content": content,
        "truncated": truncated,
        "cached": cached,
    }
    if title:
        payload["title"] = title
    if note:
        payload["note"] = note
    return payload


def _error_from(fetch_result: dict, context: str) -> dict:
    """Convert a fetcher failure dict into the tool-level error shape.

    ``candidates`` and ``available_tags`` are carried through: an error that
    names what *does* exist is a different error from one that only says no.
    """
    out: dict = {"error": f"{context}: {fetch_result.get('error', 'unknown failure')}"}
    if fetch_result.get("suggestion"):
        out["suggestion"] = fetch_result["suggestion"]
    else:
        out["suggestion"] = "retry, or check the identifier/coordinates"
    for key in ("candidates", "available_tags"):
        if fetch_result.get(key):
            out[key] = fetch_result[key]
    return out


def _probe_endpoint(url: str) -> dict:
    """Light GET probe through the politeness layer; never raises.

    The old version fell back to a ``curl`` subprocess for "edges that stall
    Python TLS clients". That fallback is gone (see :mod:`.fetchers`) and a
    health check that ignored robots.txt, throttle and retry would be a hole in
    the layer anyway — a status tool that hammers the site is not a fix.
    """
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            response = get_politeness().get(client, url)
        if response.blocked_by_robots or response.error:
            return {
                "status": "error",
                "http_status": response.status_code,
                "error": response.error or "blocked by robots.txt",
            }
        status_code = int(response.status_code) if response.status_code is not None else 0
        return {"status": "ok" if status_code < 400 else "error", "http_status": status_code}
    except Exception as exc:
        return {"status": "error", "http_status": None, "error": f"{type(exc).__name__}: {exc}"}


def _robots_cache_rows() -> int | None:
    """How many robots.txt records the politeness SQLite cache holds (read-only).

    ``0`` when the file does not exist (the layer then runs on its in-memory
    fallback — politeness still applies, it just forgets across restarts);
    ``None`` when the path itself could not be resolved.
    """
    try:
        path = default_robots_db_path()
    except Exception:
        return None
    try:
        import os

        if not os.path.exists(path):
            return 0
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM robots").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


def _politeness_report() -> dict:
    """Politeness counters for ``java_status``; never raises.

    Deliberately *outside* ``checks``: it is diagnostics, not a health signal,
    so it must not drag ``overall`` to "degraded" on its own.
    """
    try:
        stats = get_politeness().stats()
    except Exception as exc:  # pragma: no cover - defensive
        return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
    return {
        "status": "ok",
        "disabled": bool(stats.get("disabled", False)),
        "requests": int(stats.get("requests", 0)),
        # A8 F1/F3: robots attempts are counted apart from content requests;
        # ``requests + robots_requests`` is the true wire total.
        "robots_requests": int(stats.get("robots_requests", 0)),
        "robots_rows": _robots_cache_rows(),
        "robots_fetches": int(stats.get("robots_fetches", 0)),
        "robots_cache_hits": int(stats.get("robots_cache_hits", 0)),
        "robots_negative": int(stats.get("robots_negative", 0)),
        "robots_refreshed_unchanged": int(stats.get("robots_refreshed_unchanged", 0)),
        "blocked_by_robots": int(stats.get("blocked_by_robots", 0)),
        "throttle_waits": int(stats.get("throttle_waits", 0)),
        "throttle_sleep_s": round(float(stats.get("throttle_sleep_s", 0.0)), 3),
        # A8 F1: the robots subset of those waits — the evidence that a
        # robots.txt fetch waits in the same per-host queue as a page request.
        "robots_throttle_waits": int(stats.get("robots_throttle_waits", 0)),
        "robots_throttle_sleep_s": round(float(stats.get("robots_throttle_sleep_s", 0.0)), 3),
        # A8 F2/F4: hops the layer walked itself, and challenge-hook hits.
        "redirect_hops": int(stats.get("redirect_hops", 0)),
        "challenge_detected": int(stats.get("challenge_detected", 0)),
        "challenge_retries": int(stats.get("challenge_retries", 0)),
        "host_delays": stats.get("hosts", {}),
        "budgets": stats.get("budgets", {}),
        "budget_denied": int(stats.get("budget_denied", 0)),
        "conditional": int(stats.get("conditional", 0)),
        "conditional_skipped": int(stats.get("conditional_skipped", 0)),
        "revalidated_304": int(stats.get("revalidated_304", 0)),
        "retries_429": int(stats.get("retries_429", 0)),
        "retry_after_honoured": int(stats.get("retry_after_honoured", 0)),
        "retries_transport": int(stats.get("retries_transport", 0)),
        "stalls": int(stats.get("stalls", 0)),
        "errors": int(stats.get("errors", 0)),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def health_check() -> dict:
    """Check that the server is up and report its version."""
    return {"status": "ok", "server": "java-spring-mcp", "version": __version__}


@mcp.tool()
def java_docs(identifier: str, topic: str | None = None, max_tokens: int = 8000) -> dict:
    """Fetch JDK class javadoc or a Spring Boot reference section as markdown.

    ``identifier`` resolution order:
      1. "spring" or "spring:<section>" (e.g. "spring:using") → Spring Boot
         reference section;
      2. dotted names like "java.util.List" → fully-qualified class; the
         module is resolved from the search index when possible, otherwise
         java.base is tried first and then a fixed list of fallback modules;
      3. plain names like "HttpClient" → exact-name lookup in the search
         index (class/interface preferred when the name is ambiguous, with a
         "note" listing alternates).

    ``topic`` (e.g. "methods", "fields", "constructors", "nested") keeps only
    that heading section plus title/description; if no such section exists the
    full content is returned with a "note" listing the available headings.
    ``max_tokens`` truncates the final markdown to roughly that many tokens
    (estimated as len(text)//4), cut at a line boundary, and sets
    "truncated": true when applied.

    On failure returns {"error", "suggestion"} — try java_search() to find
    the right name or package.
    """
    try:
        # One request budget per tool call: the index page plus the module
        # guesses below share it, so a wrong class name can never cost more
        # than FETCH_BUDGET_LIMIT requests (A2 §2.3 measured 6, and 18 if
        # Oracle ever answered 429).
        with tool_budget("java_docs"):
            return _java_docs_impl(identifier, topic, max_tokens)
    except Exception as exc:  # last-resort guard: never let an exception escape
        return {
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry, or use java_search() to find the right identifier",
        }


def _java_docs_impl(identifier: str, topic: str | None, max_tokens: int) -> dict:
    ident = str(identifier or "").strip()
    if not ident:
        return {
            "error": "identifier is required",
            "suggestion": "pass a class name, a fully-qualified name, or 'spring[:<section>']",
        }

    # --- rule (a): Spring Boot reference sections --------------------------
    lowered = ident.lower()
    if lowered == "spring" or lowered.startswith("spring:"):
        raw_section = ident.split(":", 1)[1] if ":" in ident else ""
        section = raw_section.strip().strip("/").lower() or "index"
        url = _spring_section_url(section)
        cached_doc = _cache_get(url)
        if cached_doc is not None:
            return _doc_payload(
                doc_type="spring_section", identifier=ident, url=url,
                title=cached_doc.get("title"), markdown=cached_doc["markdown"],
                topic=topic, max_tokens=max_tokens, cached=True,
            )
        result = fetch_spring_boot_section(section)
        if not result.get("ok"):
            return _error_from(result, f"spring section '{section}' fetch failed")
        _cache_set(url, {
            "title": result.get("title"),
            "markdown": result.get("markdown"),
            "section": result.get("section", section),
        }, SPRING_DOC_TTL)
        return _doc_payload(
            doc_type="spring_section", identifier=ident, url=result.get("url") or url,
            title=result.get("title"), markdown=result.get("markdown", ""),
            topic=topic, max_tokens=max_tokens, cached=False,
        )

    index = load_index()  # never raises; may carry "error" + empty entries

    # --- rule (b): fully-qualified class names ------------------------------
    if "." in ident and ident.split(".", 1)[0] != "spring":
        module = _module_from_index(ident, index)
        first = module or "java.base"
        candidates = [first] + [m for m in _FALLBACK_MODULES if m not in (first,)]
        last_error = "no fetch attempt made"
        budget_hit = False
        for mod in candidates:
            url = _jdk_class_url(ident, mod)
            cached_doc = _cache_get(url)
            if cached_doc is not None:
                return _doc_payload(
                    doc_type="jdk_class", identifier=ident, url=url,
                    title=cached_doc.get("title"), markdown=cached_doc["markdown"],
                    topic=topic, max_tokens=max_tokens, cached=True,
                )
            result = fetch_jdk_class(ident, module=mod)
            if result.get("ok"):
                _cache_set(url, {
                    "title": result.get("title"),
                    "markdown": result.get("markdown"),
                }, JDK_DOC_TTL)
                return _doc_payload(
                    doc_type="jdk_class", identifier=ident, url=result.get("url") or url,
                    title=result.get("title"), markdown=result.get("markdown", ""),
                    topic=topic, max_tokens=max_tokens, cached=False,
                )
            last_error = result.get("error") or "unknown failure"
            if result.get("budget_exhausted"):
                # Oracle answers a wrong name with HTTP 200, so nothing in the
                # status line stops this loop — the budget does.  Trying the
                # remaining modules would only add denials, not requests.
                budget_hit = True
                break
        return {
            "error": f"could not fetch javadoc for '{ident}': {last_error}",
            "suggestion": (
                f"the per-call request budget ({FETCH_BUDGET_LIMIT}) ran out while guessing "
                f"modules; try java_search('{ident}') to find the right package/module"
                if budget_hit
                else "try java_search('...') to find the right name or package"
            ),
        }

    # --- rule (c): plain class names via the search index -------------------
    entries = index.get("entries") or []
    candidates = [e for e in entries if e.get("name") == ident]
    if not candidates:
        lowered_ident = ident.lower()
        candidates = [
            e for e in entries
            if str(e.get("name") or "").lower() == lowered_ident
        ]

    if not candidates:
        # No index match: no package to build an FQN from — suggest a search.
        close = search(ident, limit=3, index=index)
        hint = ""
        if close:
            hint = "; close matches: " + ", ".join(
                f"{e.get('package')}.{e.get('name')}" for e in close[:3]
            )
        return {
            "error": f"no class named '{ident}' found in the search index",
            "suggestion": f"try java_search('{ident}') to find the right name or package{hint}",
        }

    preferred = [e for e in candidates if e.get("kind") in ("class", "interface")]
    ordered = preferred or candidates
    chosen = ordered[0]
    alternates = [e for e in candidates if e is not chosen]
    note: str | None = None
    if len(candidates) > 1:
        alt_list = ", ".join(
            f"{e.get('package')}.{e.get('name')} (module {e.get('module')})"
            for e in alternates[:5]
        )
        note = (
            f"ambiguous name '{ident}'; chose {chosen.get('package')}.{chosen.get('name')} "
            f"(module {chosen.get('module')}, kind {chosen.get('kind')}); alternates: {alt_list}"
        )

    last_error = "no fetch attempt made"
    for entry in [chosen] + alternates:
        fqcn = f"{entry.get('package')}.{entry.get('name')}"
        mod = entry.get("module") or "java.base"
        url = _jdk_class_url(fqcn, mod)
        cached_doc = _cache_get(url)
        if cached_doc is not None:
            return _doc_payload(
                doc_type="jdk_class", identifier=ident, url=url,
                title=cached_doc.get("title"), markdown=cached_doc["markdown"],
                topic=topic, max_tokens=max_tokens, cached=True, note=note,
            )
        result = fetch_jdk_class(fqcn, module=mod)
        if result.get("ok"):
            _cache_set(url, {
                "title": result.get("title"),
                "markdown": result.get("markdown"),
            }, JDK_DOC_TTL)
            return _doc_payload(
                doc_type="jdk_class", identifier=ident, url=result.get("url") or url,
                title=result.get("title"), markdown=result.get("markdown", ""),
                topic=topic, max_tokens=max_tokens, cached=False, note=note,
            )
        last_error = result.get("error") or "unknown failure"
        if result.get("budget_exhausted"):
            break  # same reason as the module loop: the budget, not the status, stops it
    return {
        "error": f"could not fetch javadoc for '{ident}' (index match {chosen.get('package')}.{chosen.get('name')}): {last_error}",
        "suggestion": "try java_search('...') to find the right name or package",
    }


@mcp.tool()
def java_search(query: str, limit: int = 8) -> dict:
    """Search JDK classes and Spring Boot reference sections by name.

    Ranks entries of the cached search index (exact > startswith > substring >
    fuzzy per token; every token must match). Spring sections are included as
    pseudo-entries, so e.g. 'spring using' finds the "Using" section.
    Returns {"query", "count", "results": [{name, package, module, kind, url,
    score}], "index_stale"}.
    """
    try:
        with tool_budget("java_search"):
            index = load_index()
        results = search(query, limit=limit, index=index)
        out: dict = {
            "query": query,
            "count": len(results),
            "results": results,
            "index_stale": bool(index.get("stale")),
        }
        if index.get("partial"):
            # A budget-truncated index (no JDK classes, Spring sections only).
            out["index_partial"] = index.get("partial_reason") or True
        if index.get("error") and not results:
            out["note"] = f"search index unavailable: {index['error']}"
        return out
    except Exception as exc:
        return {
            "error": f"search failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry java_search()",
        }


@mcp.tool()
def maven_package(
    artifact_id: str,
    group_id: str | None = None,
    version: str | None = None,
    max_tokens: int = 6000,
) -> dict:
    """Look up an artifact on Maven Central (metadata only).

    Returns the newest matching version, the top 20 versions and project /
    javadoc links. When ``version`` is given and not found, the error carries
    a suggestion listing available versions. Metadata is cached for 1 day
    keyed by the search-API URL; failures are never cached.

    Note: ``max_tokens`` is reserved for future javadoc content fetching and
    currently has no effect — only metadata is returned.
    """
    try:
        artifact = str(artifact_id or "").strip()
        if not artifact:
            return {
                "error": "artifact_id is required",
                "suggestion": "pass e.g. artifact_id='guava' (optionally group_id)",
            }
        key = _maven_search_url(group_id, artifact)
        cache = _cache()
        if cache is not None:
            try:
                raw = cache.get(key)
            except Exception:
                raw = None
            if raw is not None:
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    data = None
                if isinstance(data, dict):
                    out = dict(data)
                    out["cached"] = True
                    return out

        # One tool call, one budget. The Maven lookup needs exactly one
        # request today; the cap is here so a future change (javadoc fetching,
        # the reserved ``max_tokens``) cannot silently fan out.
        with tool_budget("maven_package"):
            result = fetch_maven_artifact(group_id, artifact_id, version)
        if not result.get("ok"):
            return _error_from(result, "maven lookup failed")

        data: dict = {
            "group_id": result.get("group_id"),
            "artifact_id": result.get("artifact_id"),
            "version": result.get("version"),
            "versions": (result.get("versions") or [])[:20],
            "url": result.get("url"),
            "javadoc_url": result.get("javadoc_url"),
        }
        if result.get("description"):
            data["description"] = result["description"]
        _cache_set(key, data, MAVEN_META_TTL)
        data["cached"] = False
        return data
    except Exception as exc:
        return {
            "error": f"maven lookup failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry, or verify the coordinates",
        }


@mcp.tool()
def java_status() -> dict:
    """Real health check: search index, local cache and upstream endpoints.

    The ``cache`` check gains ``read_only: true`` plus ``read_only_reason``
    when the database cannot be written (P5).  It stays an "ok" check on
    purpose: a cache that only reads is a degraded optimisation, not a sick
    server, so ``overall`` does not change.

    Probes docs.oracle.com, docs.spring.io and search.maven.org with a light
    GET (10s timeout each). Never raises; returns {"server", "version",
    "checks": {...}, "overall": "ok"|"degraded"|"error"} where overall is
    "error" when the search index is unavailable, "degraded" when any check
    fails, and "ok" otherwise.

    Also returns a top-level ``politeness`` block with the politeness layer's
    counters: robots cache rows/fetches/hits, requests blocked by robots.txt,
    throttle waits and per-host delays, conditional GETs / 304 revalidations,
    429 / transport retries, stalls, request budgets and whether the layer is
    disabled (``JAVA_SPRING_MCP_POLITENESS_DISABLED``). It sits outside
    ``checks`` on purpose: it is diagnostics and must never change
    ``overall``.
    """
    try:
        checks: dict = {}

        index = load_index()
        entry_count = len(index.get("entries") or [])
        checks["search_index"] = {
            "status": "ok" if entry_count else "error",
            "entries": entry_count,
            "built_at": index.get("built_at"),
            "stale": bool(index.get("stale")),
        }

        try:
            cache = DocCache()
            stats = cache.stats()
            checks["cache"] = {
                "status": "ok",
                "entries": int(stats.get("entries", 0)),
                "expired": int(stats.get("expired", 0)),
            }
            if cache.read_only:
                # P5: a read-only cache is a degraded optimisation, not a
                # broken server.  It is announced here, and the check keeps
                # ``status: "ok"`` on purpose so ``overall`` is unchanged.
                checks["cache"]["read_only"] = True
                checks["cache"]["read_only_reason"] = cache.read_only_reason
        except Exception as exc:
            checks["cache"] = {
                "status": "error",
                "entries": 0,
                "expired": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

        for name, url in _ENDPOINT_PROBES:
            checks[name] = _probe_endpoint(url)

        if checks["search_index"]["status"] == "error":
            overall = "error"
        elif any(c.get("status") != "ok" for c in checks.values()):
            overall = "degraded"
        else:
            overall = "ok"
        return {
            "server": "java-spring-mcp",
            "version": __version__,
            "checks": checks,
            "overall": overall,
            # Outside "checks": diagnostics, never a health signal (A3 §5.6).
            "politeness": _politeness_report(),
        }
    except Exception as exc:
        return {
            "error": f"status check failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry java_status()",
        }


# ---------------------------------------------------------------------------
# Spring documentation tools
# ---------------------------------------------------------------------------

#: Reference pages ``spring_search_concepts`` reads per call for in-page
#: headings and excerpts.  Sized for the budget, not for ambition: index page
#: (1) + these (4) = 5 of :data:`FETCH_BUDGET_LIMIT` 6, leaving one unit for a
#: 429 retry.  With ``docs.spring.io``'s ``crawl-delay: 1`` a cold search costs
#: about five seconds — once a week, because the headings are persisted.
_CONCEPT_EXCERPT_PAGES = 2

#: How many Initializr catalogue matches ``spring_dependency`` turns into real
#: build files.  Four keeps the generated pom readable and the two build-file
#: requests well inside the budget.
_DEPENDENCY_MATCH_LIMIT = 4


def _bounded_int(value, low: int, high: int, default: int) -> int:
    """Coerce a tool argument into ``[low, high]`` without raising."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _truncate_list(items: list[dict], max_tokens: int | None) -> tuple[list[dict], bool]:
    """Keep whole list items until the token budget (``len // 4``) is spent.

    The list analogue of :func:`_truncate_markdown`: same estimate, same
    "never cut an item in half" discipline — a catalogue entry is either in the
    answer or it is not, and ``truncated`` says which happened.  Always keeps at
    least one item, so a tiny budget still returns a usable answer.
    """
    if not max_tokens or int(max_tokens) <= 0:
        return items, False
    budget = int(max_tokens) * 4
    kept: list[dict] = []
    used = 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False))
        if kept and used + size > budget:
            return kept, True
        kept.append(item)
        used += size
    return kept, False


@mcp.tool()
def spring_search_concepts(
    query: str,
    version: str | None = None,
    limit: int = 8,
    max_tokens: int = 4000,
) -> dict:
    """Find where a Spring Boot topic is documented: section titles and headings.

    Matches the documentation's own titles ("Auto-configuration", "Profiles",
    "Web", "Security") **and** the h2/h3/h4 headings inside pages, so "data jpa"
    finds reference/data/sql.html#jpa-and-spring-data even though no page is
    titled "JPA".  Each hit carries title, url, section, version, score,
    matched_on ("title", "path" or "heading") and a short excerpt.

    ``version`` (e.g. "4.1") searches that docs line; omitted searches the
    latest one and the result reports which version that was.  ``limit`` caps
    hits (1-25).  ``max_tokens`` bounds the excerpt text across the hits
    (estimated as len(text)//4): whole hits past the budget are dropped and
    "truncated" is set.

    The index is a single page — the reference navigation tree (296 entries on
    the measured 4.1.1 docs) — cached for a week.  Headings are harvested only
    for queries the titles cannot answer, at most 4 pages per call, and are
    persisted under their own cache key, so a repeat search costs zero requests.

    On failure returns {"error", "suggestion"}.
    """
    try:
        with tool_budget("spring_search_concepts"):
            return _spring_search_concepts_impl(query, version, limit, max_tokens)
    except Exception as exc:  # last-resort guard: never let an exception escape
        return {
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry, or call spring_search_concepts('actuator') to test the index",
        }


def _spring_search_concepts_impl(
    query: str, version: str | None, limit, max_tokens
) -> dict:
    text = str(query or "").strip()
    if not text:
        return {
            "error": "query is required",
            "suggestion": (
                "e.g. spring_search_concepts('auto-configuration'), "
                "('profiles'), ('actuator'), ('data jpa')"
            ),
        }
    limit = _bounded_int(limit, 1, 25, 8)

    index = spring.load_concept_index(version)
    if not index.get("ok"):
        return _error_from(index, "the Spring concept index could not be built")
    entries = index.get("entries") or []
    docs_version = index.get("version") or version or "latest"

    nav_hits = spring.rank_entries(text, entries)
    partial = False
    if not nav_hits:
        # Nothing matches every word.  Keep the ranked partials as hints rather
        # than returning an empty list — but say what they are.
        nav_hits = spring.rank_entries(text, entries, require_all=False)
        partial = bool(nav_hits)

    headings_store = spring.load_heading_store()
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(entry: dict, heading: dict | None = None, source: str = "nav") -> None:
        url = entry.get("url") or ""
        anchor = (heading or {}).get("anchor") or ""
        key = (url, anchor)
        if key in seen:
            return
        seen.add(key)
        hit: dict = {
            "title": (heading or {}).get("text") or entry.get("title") or "",
            "url": f"{url}#{anchor}" if anchor else url,
            "section": entry.get("section") or "",
            "version": docs_version,
            "score": (heading or entry).get("score") or 0.0,
            "matched_on": "heading" if heading else entry.get("matched_on", "path"),
            "excerpt": (heading or {}).get("excerpt") or entry.get("excerpt") or "",
            "excerpt_source": source if hit_excerpt_available(entry, heading) else "not fetched",
            # Kept in the payload because they are what the ranking tie-breaks
            # on, and a caller can see why a page outranked its own subpages.
            "nav_depth": entry.get("depth", 99),
            "is_reference_page": _is_reference_url(url),
        }
        if heading:
            hit["heading_level"] = heading.get("level") or ""
            hit["page_title"] = entry.get("title") or ""
        hits.append(hit)

    # Offline pass: nav titles, plus headings an earlier call already harvested.
    for entry in nav_hits:
        stored = headings_store.get(entry["url"]) or {}
        source = "nav"
        if stored.get("summary"):
            # The excerpt is page prose, not a guess from the title, so it is
            # labelled where it came from.
            entry = {**entry, "excerpt": stored["summary"]}
            source = "page"
        for heading in spring.rank_headings(
            text, stored.get("headings") or [], entry, limit=3
        ):
            add(entry, heading, source="page")
        add(entry, None, source=source)

    # Which pages this query would have to read.  Computed here, before the
    # store is consulted, so that a warm answer and a cold answer are built the
    # same way: the same pages, the same heading limit, the same page hit.
    reference_hits = [h for h in nav_hits if h.get("is_reference")]
    pages = (
        reference_hits[:_CONCEPT_EXCERPT_PAGES]
        if reference_hits
        else spring.candidate_pages(text, entries)
    )
    pages = pages[: spring.HEADING_PAGE_LIMIT]
    page_urls = {page["url"] for page in pages}

    # A page this query wants is already in the store: answer from the store
    # instead of fetching it again.  Without this pass a second call answers
    # with fewer hits than the first one, because the hits that came from
    # reading the page were never looked at again.
    for page in pages:
        stored = headings_store.get(page["url"])
        if not stored:
            continue
        page_info = {**page, "excerpt": stored.get("summary") or ""}
        for heading in spring.rank_headings(
            text, stored.get("headings") or [], page_info, limit=3
        ):
            add(page_info, heading, source="page")
        add(page_info, None, source="page")

    # Every other harvested page, scanned by heading.  A query like
    # "connection pool" matches no nav title at all, but the page that holds
    # that heading was read by an earlier search, so its headings can answer
    # the query without spending a single request.  Only heading hits are added
    # here: a page nobody ranked has no honest page score to give it.
    if headings_store:
        by_url = {e["url"]: e for e in entries}
        harvested: list[tuple[float, dict, dict]] = []
        for url, stored in headings_store.items():
            if url in page_urls:
                continue  # the pass above already used this page
            base = by_url.get(url)
            if base is None:
                continue  # the page is no longer in the nav: nothing to link to
            entry = {**base, "excerpt": stored.get("summary") or ""}
            for heading in spring.rank_headings(
                text, stored.get("headings") or [], entry, limit=2
            ):
                harvested.append((heading["score"], entry, heading))
        harvested.sort(key=lambda item: (-item[0], item[1]["depth"]))
        for _, entry, heading in harvested[:limit]:
            add(entry, heading, source="page")

    # Online pass, only where the titles could not answer the question.
    pages_read = 0
    headings_found = 0
    budget_exhausted = False
    for page in pages:
        if page["url"] in headings_store:
            continue  # harvested before: the offline pass already used it
        result = spring.fetch_reference_page(_nav_rel_path(page["path"]), version)
        if not result.get("ok"):
            if result.get("budget_exhausted"):
                budget_exhausted = True
            continue
        pages_read += 1
        page_info = {
            **page,
            "url": result["url"],
            "title": result.get("title") or page.get("title"),
        }
        headings = result.get("headings") or []
        headings_found += len(headings)
        page_info["excerpt"] = _page_summary(headings, result.get("lede") or "")
        headings_store[result["url"]] = {
            "headings": headings,
            "summary": page_info["excerpt"],
        }
        for heading in spring.rank_headings(text, headings, page_info, limit=3):
            add(page_info, heading, source="page")
        add(page_info, None, source="page")
        # The page itself was already a hit from the nav, where no body text
        # existed yet; now that the page has been read, its hits quote it.
        for hit in hits:
            if hit["url"] == page_info["url"] and not hit["excerpt"]:
                hit["excerpt"] = page_info["excerpt"]
                hit["excerpt_source"] = "page"

    if pages_read:
        spring.save_heading_store(headings_store)

    # Ties go to the shallower page, then to a title match: "actuator" scores
    # 4.14 on all 13 actuator pages through their section, and the answer the
    # caller wants is the section page, not "Auditing".
    hits.sort(
        key=lambda h: (
            -h["score"],
            h["nav_depth"],
            0 if h["matched_on"] == "title" else 1,
            h["title"].lower(),
        )
    )
    hits = hits[:limit]
    hits, truncated = _truncate_list(hits, max_tokens)

    payload: dict = {
        "query": text,
        "version": docs_version,
        "docs_base": index.get("base_url") or spring.reference_base(version),
        "hit_count": len(hits),
        "hits": hits,
        "index": {
            "entries": index.get("entry_count") or len(entries),
            "built_at": index.get("built_at"),
            "cached": bool(index.get("cached")),
            "stale": bool(index.get("stale")),
        },
        "pages_read": pages_read,
        "headings_harvested": headings_found,
        "truncated": truncated,
    }
    notes: list[str] = []
    if partial:
        notes.append(
            "no documentation page matches every word; hits are partial matches"
        )
    if budget_exhausted:
        notes.append(
            "the request budget ran out before every candidate page could be read"
        )
    if index.get("stale"):
        notes.append(f"index served stale: {index.get('stale_reason')}")
    if notes:
        payload["note"] = "; ".join(notes)
    return payload


def hit_excerpt_available(entry: dict, heading: dict | None) -> bool:
    """Whether this hit already carries excerpt text (as opposed to a bare title)."""
    if heading:
        return bool(heading.get("excerpt"))
    return bool(entry.get("excerpt"))


def _nav_rel_path(path: str) -> str:
    """``/spring-boot/reference/data/sql.html`` → ``"data/sql"``.

    Only reference paths reach here (:func:`java_spring_mcp.spring.candidate_pages`
    filters the rest), so stripping the prefix is enough to rebuild the URL.
    """
    rel = str(path)
    if rel.startswith("/spring-boot/reference/"):
        rel = rel[len("/spring-boot/reference/") :]
    return re.sub(r"\.html$", "", rel)


def _page_summary(headings: list[dict] | None, lede: str = "") -> str:
    """A page's own summary: the prose under its first heading that has any.

    The headings were parsed while fetching the page, so this costs nothing —
    and it is the same text a heading hit quotes, keeping a page hit and its
    heading hits consistent.  Section landing pages have no headings at all, so
    the opening paragraph (``lede``) is the fallback.
    """
    for heading in headings or []:
        if heading.get("excerpt"):
            return heading["excerpt"]
    return lede


def _is_reference_url(url: str) -> bool:
    """Whether a hit is a page of the reference proper (not appendix/ or api/)."""
    return "/spring-boot/reference/" in (url or "")


@mcp.tool()
def spring_reference(
    section: str,
    subsection: str | None = None,
    version: str | None = None,
    max_tokens: int = 8000,
) -> dict:
    """Read a Spring Boot reference section or subsection as clean markdown.

    ``section`` / ``subsection`` are resolved against the documentation's own
    navigation tree, so "web", "data"+"sql", "using"+"auto-configuration",
    "how-to"+"actuator" and "installing" all work, and a name that does not
    exist comes back with the sections that do.  Resolution is offline when the
    index is cached, so no request is wasted guessing a URL and getting a 404.

    When ``subsection`` is not a page of its own, the section page is returned
    with only the matching heading section kept (plus title and description) —
    the same behaviour as ``java_docs(topic=...)`` — and a "note" lists the
    available headings when nothing matches.

    ``version`` (e.g. "4.1") reads that docs line; omitted reads the latest.
    ``max_tokens`` truncates the markdown to roughly that many tokens
    (estimated as len(text)//4) at a line boundary and sets "truncated".

    On failure returns {"error", "suggestion", "candidates"}.
    """
    try:
        with tool_budget("spring_reference"):
            return _spring_reference_impl(section, subsection, version, max_tokens)
    except Exception as exc:
        return {
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry, or spring_search_concepts('<topic>') to find the section",
        }


def _spring_reference_impl(
    section: str, subsection: str | None, version: str | None, max_tokens: int
) -> dict:
    resolved = spring.resolve_reference_url(section, subsection, version)
    topic_fallback: str | None = None
    if not resolved.get("ok"):
        if not subsection:
            return _error_from(resolved, "unknown Spring documentation section")
        # The subsection is not a page.  Fall back to the section page and filter
        # its headings the way java_docs(topic=...) does.
        resolved = spring.resolve_reference_url(section, None, version)
        if not resolved.get("ok"):
            return _error_from(resolved, "unknown Spring documentation section")
        topic_fallback = subsection

    page = spring.fetch_reference_url(resolved["url"])
    if not page.get("ok"):
        return _error_from(page, f"could not fetch {resolved['url']}")

    identifier = section if not subsection else f"{section}:{subsection}"
    notes = [n for n in (resolved.get("note"),) if n]
    if topic_fallback:
        notes.append(
            f"{subsection!r} is not a page of its own; filtered {resolved['title']!r} by "
            "heading — spring_search_concepts("
            + repr(subsection)
            + ") finds the page that holds it"
        )
    payload = _doc_payload(
        doc_type="spring-reference",
        identifier=identifier,
        url=page["url"],
        title=page.get("title") or resolved.get("title"),
        markdown=page.get("markdown") or "",
        topic=topic_fallback,
        max_tokens=max_tokens,
        cached=bool(page.get("cached")),
        note="; ".join(notes) or None,
    )
    payload["version"] = version or resolved.get("index_version") or "latest"
    payload["headings"] = [
        f"{h['level']} {h['text']}" for h in page.get("headings") or []
    ]
    if resolved.get("alternatives"):
        payload["alternatives"] = resolved["alternatives"]
    return payload


@mcp.tool()
def spring_guides(
    guide: str | None = None,
    topic: str | None = None,
    max_tokens: int = 8000,
) -> dict:
    """List the spring.io guides, or read one guide as markdown.

    No ``guide`` → the catalogue (title, url, type, category, description) from
    spring.io's own guide data: the HTML index is rendered client-side, so
    scraping it yields no guides at all, and the sitemap fallback yields URLs
    without titles.  ``topic`` filters the catalogue by title, slug, category or
    description.

    ``guide`` → that guide as markdown.  Guide *pages* are server-rendered even
    though the index is not, so "gs/spring-boot", "accessing-data-jpa" or a full
    https://spring.io/guides/… URL all work.  ``topic`` then keeps only the
    matching heading section, as ``java_docs(topic=...)`` does.  ``max_tokens``
    truncates (estimated as len(text)//4).

    On failure returns {"error", "suggestion"}.
    """
    try:
        with tool_budget("spring_guides"):
            return _spring_guides_impl(guide, topic, max_tokens)
    except Exception as exc:
        return {
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry spring_guides() to list the guides",
        }


def _spring_guides_impl(guide: str | None, topic: str | None, max_tokens: int) -> dict:
    if guide:
        fetched = spring.fetch_guide_page(guide)
        if not fetched.get("ok"):
            return _error_from(fetched, "could not fetch that guide")
        payload = _doc_payload(
            doc_type="spring-guide",
            identifier=str(guide).strip(),
            url=fetched["url"],
            title=fetched.get("title"),
            markdown=fetched.get("markdown") or "",
            topic=topic,
            max_tokens=max_tokens,
            cached=bool(fetched.get("cached")),
            note=fetched.get("description") or None,
        )
        return payload

    catalogue = spring.load_guide_catalogue()
    if not catalogue.get("ok"):
        return _error_from(catalogue, "the spring.io guide catalogue is unavailable")

    guides = catalogue.get("guides") or []
    note = catalogue.get("note")
    if topic:
        needles = [n for n in str(topic).lower().split() if n]
        guides = [
            g
            for g in guides
            if any(
                needle in " ".join(
                    [g.get("title", ""), g.get("slug", ""), g.get("description", "")]
                    + list(g.get("category") or [])
                ).lower()
                for needle in needles
            )
        ]
        if not guides:
            return {
                "error": f"no spring.io guide matches {topic!r}",
                "suggestion": "spring_guides() with no topic lists every guide",
            }
    guides, truncated = _truncate_list(guides, max_tokens)
    payload = {
        "type": "spring-guide-catalogue",
        "source": catalogue.get("source"),
        "url": catalogue.get("source_url"),
        "guide_count": len(guides),
        "guides": guides,
        "truncated": truncated,
        "cached": bool(catalogue.get("cached")),
    }
    if note:
        payload["note"] = note
    if catalogue.get("stale"):
        payload["note"] = f"{payload.get('note', '')}; catalogue served stale".strip("; ")
    return payload


@mcp.tool()
def spring_initializr(
    section: str = "options",
    query: str | None = None,
    max_tokens: int = 6000,
) -> dict:
    """Read Spring Initializr's own metadata: project options or dependency catalogue.

    ``section="options"`` → the choices start.spring.io offers (Spring Boot
    versions with the newest *released* one called out, Java versions, project
    types, packaging, and the defaults for groupId/artifactId/version).  No
    version is hardcoded anywhere: it is whatever Initializr is serving today.

    ``section="dependencies"`` → the dependency catalogue (id, name,
    description, group, reference doc link), filtered by ``query`` over those
    fields.  Without ``query`` the whole catalogue is returned, truncated to
    ``max_tokens``.

    On failure returns {"error", "suggestion"}.
    """
    try:
        with tool_budget("spring_initializr"):
            return _spring_initializr_impl(section, query, max_tokens)
    except Exception as exc:
        return {
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry spring_initializr('options')",
        }


def _spring_initializr_impl(section: str, query: str | None, max_tokens: int) -> dict:
    fetched = spring.fetch_initializr_metadata()
    if not fetched.get("ok"):
        return _error_from(fetched, "Spring Initializr metadata is unavailable")
    try:
        metadata = json.loads(fetched["text"])
    except (TypeError, ValueError):
        return {
            "error": "Spring Initializr returned a non-JSON body",
            "suggestion": "check https://start.spring.io/ availability, then retry",
        }
    parsed = spring.parse_initializr_metadata(metadata)
    if not parsed:
        return {
            "error": "Spring Initializr metadata has an unexpected shape",
            "suggestion": "check https://start.spring.io/ — the v2.1 metadata format may have changed",
        }

    which = str(section or "options").strip().lower()
    boot = parsed["boot_versions"]
    base = {
        "source": "start.spring.io",
        "url": fetched["url"],
        "cached": bool(fetched.get("cached")),
        "boot_version_default": boot.get("default"),
        "boot_version_latest_released": boot.get("latest_released"),
    }

    if which in {"option", "options"}:
        base["type"] = "initializr-options"
        base["boot_versions"] = boot
        base["java_versions"] = parsed["java_versions"]
        base["languages"] = parsed["languages"]
        base["project_types"] = parsed["types"]
        base["packagings"] = parsed["packagings"]
        base["defaults"] = parsed["defaults"]
        base["dependency_groups"] = parsed["dependency_groups"]
        base["dependency_count"] = parsed["dependency_count"]
        return base

    if which in {"dependency", "dependencies"}:
        catalogue = parsed["dependencies"]
        if query:
            catalogue = spring.rank_dependencies(str(query), catalogue)
            if not catalogue:
                return {
                    "error": f"no Initializr dependency matches {query!r}",
                    "suggestion": (
                        "spring_initializr('dependencies') with no query lists every "
                        "id, and spring_dependency('<need>') maps a need to starters"
                    ),
                    "dependency_groups": parsed["dependency_groups"],
                }
        catalogue, truncated = _truncate_list(catalogue, max_tokens)
        base["type"] = "initializr-dependencies"
        base["dependency_count"] = len(catalogue)
        base["dependencies"] = catalogue
        base["truncated"] = truncated
        return base

    return {
        "error": f"unknown section {section!r}",
        "suggestion": "section is 'options' or 'dependencies'",
    }


@mcp.tool()
def spring_dependency(
    need: str,
    build: str = "both",
    boot_version: str | None = None,
    max_tokens: int = 4000,
) -> dict:
    """Turn a need ("jpa", "web", "oauth2 client") into real starter coordinates.

    No artifact name is invented.  The need is matched against Spring
    Initializr's own dependency catalogue, then Initializr is asked to generate
    the Maven pom and the Gradle build file for those ids, and **those** are
    returned — parsed coordinates plus ready-to-paste snippets.  That matters:
    the catalogue id "web" generates ``spring-boot-starter-webmvc`` on Spring
    Boot 4.1, not the ``spring-boot-starter-web`` that string-building
    ``spring-boot-starter-<id>`` would produce.

    ``build`` is "both" (default), "maven" or "gradle".  ``boot_version`` pins
    the line (plain form, e.g. "4.1.1"); omitted uses Initializr's newest
    released default.  ``max_tokens`` truncates the snippets.

    On failure returns {"error", "suggestion"} — an unmatched need returns an
    error rather than a guess.
    """
    try:
        with tool_budget("spring_dependency"):
            return _spring_dependency_impl(need, build, boot_version, max_tokens)
    except Exception as exc:
        return {
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry spring_dependency('jpa')",
        }


def _spring_dependency_impl(
    need: str, build: str, boot_version: str | None, max_tokens: int
) -> dict:
    text = str(need or "").strip()
    if not text:
        return {
            "error": "need is required",
            "suggestion": "e.g. spring_dependency('jpa'), ('web'), ('oauth2 client')",
        }

    fetched = spring.fetch_initializr_metadata()
    if not fetched.get("ok"):
        return _error_from(fetched, "Spring Initializr metadata is unavailable")
    try:
        metadata = json.loads(fetched["text"])
    except (TypeError, ValueError):
        return {
            "error": "Spring Initializr returned a non-JSON body",
            "suggestion": "check https://start.spring.io/ availability, then retry",
        }
    parsed = spring.parse_initializr_metadata(metadata)
    if not parsed:
        return {
            "error": "Spring Initializr metadata has an unexpected shape",
            "suggestion": "check https://start.spring.io/ — the v2.1 metadata format may have changed",
        }

    matches = spring.rank_dependencies(text, parsed["dependencies"])
    if not matches:
        return {
            "error": f"no Spring Initializr dependency matches {text!r}",
            "suggestion": (
                "spring_initializr('dependencies') lists every id in the catalogue; "
                "this tool only recommends artifacts that catalogue offers"
            ),
            "dependency_groups": parsed["dependency_groups"],
        }

    chosen = matches[:_DEPENDENCY_MATCH_LIMIT]
    ids = [m["id"] for m in chosen]
    effective = spring.plain_boot_version(
        boot_version or parsed["boot_versions"].get("latest_released")
        or parsed["boot_versions"].get("default")
    )

    which = str(build or "both").strip().lower()
    if which not in {"both", "maven", "gradle"}:
        which = "both"

    build_files = spring.fetch_initializr_build_files(ids, effective)
    if not build_files.get("ok"):
        # The catalogue answer is still the honest one; only the generated
        # snippets are missing, so say exactly that instead of failing the call.
        return {
            "type": "spring-dependency",
            "need": text,
            "boot_version": effective or "Initializr default",
            "matches": chosen,
            "coordinates": [],
            "note": (
                "the matches are from the Initializr catalogue, but Initializr's "
                f"generated build files could not be fetched ({build_files['error']}), "
                "so no snippets are returned"
            ),
        }

    coordinates = build_files.get("coordinates") or []
    gradle_coordinates = build_files.get("gradle_coordinates") or []
    maven_block = spring.maven_snippet(coordinates)
    gradle_block = spring.gradle_snippet(gradle_coordinates)
    if which == "maven":
        gradle_block = ""
    elif which == "gradle":
        maven_block = ""
    maven_block, maven_truncated = _truncate_markdown(maven_block, max_tokens)
    gradle_block, gradle_truncated = _truncate_markdown(gradle_block, max_tokens)

    payload: dict = {
        "type": "spring-dependency",
        "need": text,
        "boot_version": effective or build_files.get("boot_version") or "Initializr default",
        "matches": chosen,
        "coordinates": coordinates if which != "gradle" else gradle_coordinates,
        "source": {
            "pom_url": build_files.get("pom_url"),
            "gradle_url": build_files.get("gradle_url"),
            "cached": bool(build_files.get("cached")),
        },
        "maven_snippet": maven_block,
        "gradle_snippet": gradle_block,
        "truncated": bool(maven_truncated or gradle_truncated),
        "note": (
            "coordinates are the ones Spring Initializr itself generates for these "
            "dependency ids, not names derived from the ids"
        ),
    }
    return payload


@mcp.tool()
def spring_versions(
    project: str = "spring-boot",
    version: str | None = None,
    focus: str = "all",
    max_tokens: int = 6000,
) -> dict:
    """Release and migration notes for a Spring project, from its GitHub releases.

    ``project`` is "spring-boot" (default), "spring-framework",
    "spring-security", "spring-data-jpa", "spring-batch" or "spring-ai".
    ``version`` picks a release ("4.1.1", or "4.1" for its newest patch);
    omitted picks the newest **released** version — never a milestone or
    snapshot, and never a hardcoded number: it is whatever GitHub lists today.

    ``focus`` is "all", "breaking-changes" (the release's *Attention Required* /
    *Noteworthy Changes* sections), "new-features" (*New Features*) or
    "deprecations".  Deprecations are found by keyword across every section
    because no spring-boot release body has a deprecation heading (measured over
    100 releases).  Migration-guide and release-notes wiki links found in the
    body are returned under ``wiki_links``.

    ``max_tokens`` truncates the rendered notes (estimated as len(text)//4).
    Releases are cached for a day: unauthenticated GitHub allows 60 requests per
    hour per IP, and one call spends at most one of them.

    On failure returns {"error", "suggestion"}.
    """
    try:
        with tool_budget("spring_versions"):
            return _spring_versions_impl(project, version, focus, max_tokens)
    except Exception as exc:
        return {
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry spring_versions('spring-boot')",
        }


def _spring_versions_impl(
    project: str, version: str | None, focus: str, max_tokens: int
) -> dict:
    result = spring.fetch_github_releases(project)
    if not result.get("ok"):
        return _error_from(result, "GitHub releases are unavailable for that project")

    releases = [spring.parse_release(r) for r in result.get("releases") or []]
    if not releases:
        return {
            "error": f"{result['repo']} has no releases GitHub will list",
            "suggestion": "check the repository name on github.com/spring-projects",
        }
    stable = [r for r in releases if not r["prerelease"]]

    target: dict | None = None
    if version:
        needle = spring.normalize_version_tag(version)
        exact = [r for r in releases if r["tag"].lower() == needle.lower()]
        if exact:
            target = exact[0]
        else:
            # "4.1" means "the newest 4.1.x", which is how people name a line.
            prefixed = [
                r for r in (stable or releases) if r["tag"].lstrip("vV").startswith(needle)
            ]
            if prefixed:
                target = prefixed[0]
    else:
        target = (stable or releases)[0]

    if target is None:
        return {
            "error": f"no {result['repo']} release matches version {version!r}",
            "suggestion": "spring_versions() with no version lists the newest one",
            "available_tags": [r["tag"] for r in releases[:12]],
        }

    focus_value = str(focus or "all").strip().lower()
    if focus_value not in {"all", "breaking-changes", "new-features", "deprecations"}:
        return {
            "error": f"unknown focus {focus!r}",
            "suggestion": "focus is 'all', 'breaking-changes', 'new-features' or 'deprecations'",
        }

    grouped = spring.filter_release_sections(target["sections"], focus_value)
    content = _render_release_notes(target, grouped)
    content, truncated = _truncate_markdown(content, max_tokens)

    payload: dict = {
        "type": "spring-release-notes",
        "project": str(project).strip().lower(),
        "repo": result["repo"],
        "tag": target["tag"],
        "name": target["name"],
        "published_at": target["published_at"],
        "url": target["url"],
        "prerelease": target["prerelease"],
        "focus": focus_value,
        "section_headings": [s["heading"] for s in target["sections"]],
        "counts": {kind: len(bullets) for kind, bullets in grouped.items()},
        "wiki_links": target["wiki_links"],
        "latest_stable": stable[0]["tag"] if stable else None,
        "latest_listed": releases[0]["tag"],
        "content": content,
        "truncated": truncated,
        "cached": bool(result.get("cached")),
    }
    if focus_value == "deprecations" and not grouped.get("deprecations"):
        payload["note"] = (
            "no bullet in this release mentions a deprecation, removal or end of "
            "support; release bodies have no deprecation heading"
        )
    return payload


def _render_release_notes(release: dict, grouped: dict) -> str:
    """Render grouped release bullets as markdown for the ``content`` field.

    Deprecations come last on purpose: it is a cross-cutting *view* over the
    other sections (the same bullet also appears in its own section), so leading
    with it would push the release's actual sections past the token budget.
    """
    lines = [f"# {release['name'] or release['tag']}", "", release["url"], ""]
    for heading in ("new-features", "breaking-changes", "bug-fixes",
                    "dependency-upgrades", "documentation", "other", "deprecations"):
        bullets = grouped.get(heading) or []
        if not bullets:
            continue
        lines.append(f"## {heading.replace('-', ' ').title()}")
        lines.extend(f"- {bullet}" for bullet in bullets)
        lines.append("")
    if not any(grouped.values()):
        lines.append("_No bullets in the requested focus._")
    return "\n".join(lines).rstrip()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
