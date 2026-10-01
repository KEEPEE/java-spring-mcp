"""Search index over JDK classes and Spring Boot reference sections.

Builds a flat list of searchable entries:

- every class/interface/enum/record/annotation listed on the Oracle
  ``allclasses-index.html`` page, with its **module** recorded (essential for
  resolving the right javadoc URL later, e.g. ``java.util.List`` lives in
  ``java.base`` while ``java.net.http.HttpClient`` lives in ``java.net.http``);
- a fixed set of Spring Boot reference sections as pseudo-entries with
  ``kind="spring_section"`` and ``module="spring-boot"``.

Public entry points:

- ``build_index() -> dict`` — fetch + parse (touches the network; may raise).
- ``load_index(force_refresh=False, max_age_seconds=604800) -> dict`` —
  cached access via :class:`~java_spring_mcp.cache.DocCache`; never raises.
- ``search(query, limit=8, index=None) -> list[dict]`` — offline ranking over
  entry names; each result is a copy of the entry augmented with ``score``.

No network access happens at import time.
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .cache import DocCache
from .fetchers import JDK_API_BASE, SPRING_REFERENCE_BASE, _http_get

__all__ = [
    "INDEX_KEY",
    "ALL_CLASSES_INDEX_URL",
    "SPRING_SECTIONS",
    "DEFAULT_MAX_AGE_SECONDS",
    "build_index",
    "load_index",
    "search",
    "parse_allclasses_html",
]

INDEX_KEY = "search-index"
ALL_CLASSES_INDEX_URL = f"{JDK_API_BASE}/allclasses-index.html"
DEFAULT_MAX_AGE_SECONDS = 604800  # one week

#: Fixed Spring Boot reference sections (static, verified to exist).
SPRING_SECTIONS = [
    "index",
    "actuator",
    "data",
    "features",
    "io",
    "messaging",
    "packaging",
    "security",
    "testing",
    "using",
    "web",
]

#: The allclasses-index table tags every row with a tab class selecting which
#: filter tab shows it; that is the most precise kind signal available.
_TAB_KIND = {
    "all-classes-table-tab1": "interface",  # Interfaces
    "all-classes-table-tab2": "class",      # Classes
    "all-classes-table-tab3": "enum",       # Enum Classes
    "all-classes-table-tab4": "record",     # Record Classes
    "all-classes-table-tab5": "class",      # Exception Classes (still classes)
    "all-classes-table-tab6": "annotation",  # Annotation Interfaces
}

# href shape: {module}/{package-path...}/{ClassName}.html — nested classes use
# dots in the file name (e.g. AbstractDocument.AttributeContext.html).
_HREF_RE = re.compile(
    r"^([A-Za-z0-9._\-]+)/((?:[A-Za-z0-9._\-]+/)+)([A-Za-z0-9._\-]+)\.html$"
)


# ---------------------------------------------------------------------------
# Parsing (pure / offline)
# ---------------------------------------------------------------------------

def _kind_from_cell(cell, anchor) -> str:
    """Resolve the entry kind from the row's tab class, falling back to title."""
    for cls in cell.get("class") or []:
        if cls in _TAB_KIND:
            return _TAB_KIND[cls]
    # Fallback: the anchor title looks like "enum class in java.util".
    title = (anchor.get("title") or "").split(" in ")[0].strip().lower()
    for prefix, kind in (
        ("enum", "enum"),
        ("record", "record"),
        ("annotation", "annotation"),
        ("interface", "interface"),
    ):
        if title.startswith(prefix):
            return kind
    return "class"


def parse_allclasses_html(html: str, base_url: str = ALL_CLASSES_INDEX_URL) -> list[dict]:
    """Parse the Oracle allclasses-index page into entry dicts.

    Each entry is ``{"name", "package", "module", "kind", "url"}`` where
    ``name`` is the simple (possibly nested, dot-separated) class name and
    ``url`` is the absolute javadoc page URL.  Raises ``ValueError`` when the
    document does not look like an allclasses-index page or yields no entries.
    """
    soup = BeautifulSoup(html, "lxml")
    panel = soup.find(id="all-classes-table.tabpanel") or soup.find("main")
    if panel is None:
        raise ValueError("not an allclasses-index page (no results table found)")

    entries: list[dict] = []
    for cell in panel.select("div.col-first"):
        anchor = cell.find("a", href=True)
        if anchor is None:  # the "Class" header row has no link
            continue
        match = _HREF_RE.match(str(anchor["href"]).strip())
        if not match:
            continue  # defensive: skip anything that is not a class page link
        module, package_path, name = match.group(1), match.group(2), match.group(3)
        entries.append(
            {
                "name": name,
                "package": package_path.rstrip("/").replace("/", "."),
                "module": module,
                "kind": _kind_from_cell(cell, anchor),
                "url": urljoin(base_url, str(anchor["href"]).strip()),
            }
        )

    if not entries:
        raise ValueError("allclasses-index page parsed to zero entries")
    return entries


def _spring_section_entries() -> list[dict]:
    """Fixed pseudo-entries for the Spring Boot reference sections."""
    entries = []
    for section in SPRING_SECTIONS:
        if section == "index":
            url = f"{SPRING_REFERENCE_BASE}/index.html"
        else:
            url = f"{SPRING_REFERENCE_BASE}/{section}/index.html"
        entries.append(
            {
                "name": f"Spring Boot: {section.capitalize()}",
                "package": section,
                "module": "spring-boot",
                "kind": "spring_section",
                "url": url,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Index building / loading
# ---------------------------------------------------------------------------

def build_index() -> dict:
    """Fetch the allclasses-index page and build the full index.

    Returns ``{"built_at": <iso8601 UTC>, "entries": [...]}``.  Unlike
    :func:`load_index`, this function **may raise** (network or parse
    failures) — callers that must not raise should use :func:`load_index`.
    """
    response = _http_get(ALL_CLASSES_INDEX_URL)
    if response.status_code >= 400:
        raise RuntimeError(
            f"allclasses-index request failed with HTTP {response.status_code}"
        )
    entries = parse_allclasses_html(response.text, base_url=ALL_CLASSES_INDEX_URL)
    entries.extend(_spring_section_entries())
    return {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }


def _valid_index(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("entries"), list)


def load_index(force_refresh: bool = False, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> dict:
    """Return the search index, using the DocCache under key ``search-index``.

    - Fresh (unexpired) cache hit: returned immediately, no network.
    - Miss or ``force_refresh``: rebuild; on success the result is cached with
      a TTL of ``max_age_seconds`` and returned.
    - Rebuild failure with an old (expired/corrupt-free) cached copy present:
      the old copy is returned with ``"stale": True``.
    - Rebuild failure with no usable cache: returns
      ``{"built_at": None, "entries": [], "error": ...}``.

    Never raises.
    """
    cache: DocCache | None = None
    try:
        cache = DocCache()
    except Exception:
        cache = None  # cache unavailable; degrade to build-only behaviour

    if not force_refresh and cache is not None:
        try:
            cached = cache.get(INDEX_KEY)
        except Exception:
            cached = None
        if cached is not None:
            try:
                data = json.loads(cached)
            except (ValueError, TypeError):
                data = None  # corrupt cache value; rebuild below
            if _valid_index(data):
                return data

    try:
        index = build_index()
    except Exception as exc:
        stale_raw = None
        if cache is not None:
            try:
                stale_raw = cache.peek(INDEX_KEY)
            except Exception:
                stale_raw = None
        if stale_raw is not None:
            try:
                data = json.loads(stale_raw)
            except (ValueError, TypeError):
                data = None
            if _valid_index(data):
                data = dict(data)
                data["stale"] = True
                return data
        return {
            "built_at": None,
            "entries": [],
            "error": f"index build failed: {type(exc).__name__}: {exc}",
        }

    if cache is not None:
        try:
            cache.set(INDEX_KEY, json.dumps(index), max_age_seconds)
        except Exception:
            pass  # a cache-write failure must not break the returned index
    return index


# ---------------------------------------------------------------------------
# Search / ranking (pure / offline)
# ---------------------------------------------------------------------------

#: Per-token score tiers.  Exact > startswith > substring > fuzzy, where the
#: fuzzy tier is capped below the substring floor so a poor fuzzy match can
#: never outrank a real substring hit.
_EXACT_SCORE = 10.0
_STARTSWITH_SCORE = 8.0
_SUBSTRING_SCORE = 6.0
_FUZZY_CAP = 4.0
_FUZZY_MIN_RATIO = 0.3


def _token_score(token: str, name_lower: str) -> float:
    """Score one lowercase query token against a lowercase entry name."""
    if token == name_lower:
        return _EXACT_SCORE
    if name_lower.startswith(token):
        return _STARTSWITH_SCORE
    if token in name_lower:
        return _SUBSTRING_SCORE
    ratio = difflib.SequenceMatcher(None, token, name_lower).ratio()
    if ratio >= _FUZZY_MIN_RATIO:
        return _FUZZY_CAP * ratio
    return 0.0


def search(query: str | None, limit: int = 8, index: dict | None = None) -> list[dict]:
    """Rank index entries by how well their names match ``query``.

    Case-insensitive.  The query is split into whitespace-separated tokens;
    an entry must match **every** token (exact > startswith > substring >
    fuzzy, with pure-fuzzy matches below ratio 0.3 dropped) and its score is
    the sum of the per-token scores.  Results are copies of the matching
    entries augmented with ``score``, sorted best-first (ties broken by name).

    ``index`` may be passed explicitly (handy for tests); when omitted the
    cached index from :func:`load_index` is used, so a bare call still works
    without touching the network on cache hits.  Empty/None queries return
    ``[]``.
    """
    if query is None:
        return []
    text = str(query).strip().lower()
    tokens = text.split()
    if not tokens:
        return []

    if index is None:
        index = load_index()
    entries = index.get("entries") or []

    results: list[dict] = []
    for entry in entries:
        name_lower = str(entry.get("name") or "").lower()
        score = 0.0
        matched = True
        for token in tokens:
            token_score = _token_score(token, name_lower)
            if token_score <= 0.0:
                matched = False
                break
            score += token_score
        if matched and score > 0.0:
            item = dict(entry)
            item["score"] = round(score, 3)
            results.append(item)

    results.sort(key=lambda r: (-r["score"], r["name"].lower()))
    if limit is not None:
        results = results[: max(0, int(limit))]
    return results
