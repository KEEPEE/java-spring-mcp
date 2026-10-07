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
    """Convert a fetcher failure dict into the tool-level error shape."""
    out: dict = {"error": f"{context}: {fetch_result.get('error', 'unknown failure')}"}
    if fetch_result.get("suggestion"):
        out["suggestion"] = fetch_result["suggestion"]
    else:
        out["suggestion"] = "retry, or check the identifier/coordinates"
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
            stats = DocCache().stats()
            checks["cache"] = {
                "status": "ok",
                "entries": int(stats.get("entries", 0)),
                "expired": int(stats.get("expired", 0)),
            }
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
