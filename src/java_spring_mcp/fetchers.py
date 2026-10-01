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
"""

from __future__ import annotations

import json as _json
import os
import re
import subprocess
import tempfile
from urllib.parse import urlencode, urljoin

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as _markdownify

JDK_API_BASE = "https://docs.oracle.com/en/java/javase/26/docs/api"
SPRING_REFERENCE_BASE = "https://docs.spring.io/spring-boot/reference"
MAVEN_SEARCH_URL = "https://search.maven.org/solrsearch/select"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# Browser-ish header set; some edges (e.g. search.maven.org) also stall or
# throttle clients without a browser-like TLS/HTTP fingerprint.
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = 20.0

__all__ = [
    "fetch_jdk_class",
    "fetch_spring_boot_section",
    "fetch_maven_artifact",
    "parse_jdk_html",
    "parse_spring_section_html",
    "parse_maven_search_json",
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class _Response:
    """Minimal response shim so httpx and the curl fallback share one API."""

    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text = text
        self.url = url

    def json(self):
        return _json.loads(self.text)


def _curl_get(url: str, params: dict | None = None) -> _Response:
    """Fallback transport using the system curl binary.

    Some edges (observed on search.maven.org) stall reads for Python's TLS
    fingerprint while serving curl fine; this keeps those hosts reachable.
    """
    if params:
        url = f"{url}?{urlencode(params)}"
    body_file = None
    try:
        with tempfile.NamedTemporaryFile(prefix="jsmcp-", delete=False) as handle:
            body_file = handle.name
        cmd = [
            "curl", "-sS", "-L", "--max-time", "90",
            "-A", USER_AGENT,
            "-H", f"Accept: {HEADERS['Accept']}",
            "-H", f"Accept-Language: {HEADERS['Accept-Language']}",
            "-o", body_file,
            "-w", "%{http_code} %{url_effective}",
            url,
        ]
        # Some edges (observed on search.maven.org) stall Python-TLS clients
        # for 20-100s before answering; give the fallback room to ride it out.
        proc = subprocess.run(cmd, capture_output=True, timeout=105)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"curl failed ({proc.returncode}): {stderr[:200]}")
        meta = proc.stdout.decode("ascii", errors="replace").split()
        status_code = int(meta[0]) if meta and meta[0].isdigit() else 0
        final_url = meta[1] if len(meta) > 1 else url
        with open(body_file, "r", encoding="utf-8", errors="replace") as fh:
            return _Response(status_code, fh.read(), final_url)
    finally:
        if body_file is not None:
            try:
                os.unlink(body_file)
            except OSError:
                pass


def _http_get(url: str, params: dict | None = None) -> _Response:
    """GET with browser-ish headers.

    Tries httpx twice (retrying on transport errors and HTTP error status),
    then falls back to the system curl binary for edges that throttle or
    stall Python's TLS fingerprint (observed on search.maven.org).  Raises
    the last httpx error only when no response was ever obtained.
    """
    last_response: _Response | None = None
    last_error: Exception | None = None
    for _attempt in range(2):  # initial try + one retry
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
                resp = client.get(url, headers=HEADERS, params=params)
            candidate = _Response(resp.status_code, resp.text, str(resp.url))
        except httpx.TransportError as exc:  # includes timeouts / connection errors
            last_error = exc
            continue
        if candidate.status_code < 400:
            return candidate
        last_response = candidate

    try:
        fallback = _curl_get(url, params)
        if fallback.status_code < 400 or last_response is None:
            return fallback
    except Exception:
        pass  # curl unavailable/failed; report what httpx gave us below
    if last_response is not None:
        return last_response
    raise last_error if last_error is not None else RuntimeError(
        f"request to {url} failed"
    ) from None


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
    try:
        response = _http_get(MAVEN_SEARCH_URL, params=params)
    except Exception as exc:  # transport errors from either backend
        return {
            "ok": False,
            "error": f"Maven Central request failed: {type(exc).__name__}: {exc}",
        }

    if response.status_code != 200:
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

    try:
        response = _http_get(url)
    except Exception as exc:  # transport errors from either backend
        return {"ok": False, "error": f"request failed: {type(exc).__name__}: {exc}"}

    if response.status_code >= 400:
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

    try:
        response = _http_get(url)
    except Exception as exc:  # transport errors from either backend
        return {"ok": False, "error": f"request failed: {type(exc).__name__}: {exc}"}

    if response.status_code >= 400:
        return {
            "ok": False,
            "error": f"HTTP {response.status_code} for {url} (section '{sec}' may not exist)",
        }

    try:
        parsed = parse_spring_section_html(response.text, url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "url": url, "section": sec, **parsed}
