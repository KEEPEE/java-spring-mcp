"""Offline tests for the MCP tools in java_spring_mcp.server.

All fetchers, the search index and endpoint probes are monkeypatched — nothing
here touches the network. The cache is exercised for real against a temporary
SQLite file via JAVA_SPRING_MCP_CACHE_DIR=tmp_path.
"""

from __future__ import annotations

import re

import pytest

from java_spring_mcp import server as server_mod


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

JDK_BASE = "https://docs.oracle.com/en/java/javase/26/docs/api"

FAKE_INDEX = {
    "built_at": "2026-01-01T00:00:00+00:00",
    "entries": [
        {
            "name": "List",
            "package": "java.util",
            "module": "java.base",
            "kind": "interface",
            "url": f"{JDK_BASE}/java.base/java/util/List.html",
        },
        {
            "name": "HttpClient",
            "package": "java.net.http",
            "module": "java.net.http",
            "kind": "class",
            "url": f"{JDK_BASE}/java.net.http/java/net/http/HttpClient.html",
        },
        {
            "name": "Timer",
            "package": "java.util",
            "module": "java.base",
            "kind": "class",
            "url": f"{JDK_BASE}/java.base/java/util/Timer.html",
        },
        {
            "name": "Timer",
            "package": "javax.swing",
            "module": "java.desktop",
            "kind": "class",
            "url": f"{JDK_BASE}/java.desktop/javax/swing/Timer.html",
        },
    ],
}

LIST_MD = (
    "# List Interface\n\n"
    "A size-constrained collection.\n\n"
    "## Methods\n\n"
    "| `add(E e)` | Adds an element. |\n\n"
    "## Fields\n\n"
    "| `SIZE` | A constant. |\n"
)

TOPIC_MD = (
    "# Timer Class\n\n"
    "Starts a timer.\n\n"
    "## Methods\n\n"
    "| `start()` | Starts it. |\n\n"
    "## Fields\n\n"
    "| `count` | An int. |\n"
)


@pytest.fixture
def fake_cache_dir(tmp_path, monkeypatch):
    """Point DocCache at a fresh temp dir so tests never share state."""
    monkeypatch.setenv("JAVA_SPRING_MCP_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


def _patch_index(monkeypatch, index: dict) -> None:
    def fake_load(force_refresh: bool = False, max_age_seconds: float = 604800) -> dict:
        return dict(index)

    monkeypatch.setattr(server_mod, "load_index", fake_load)


def _patch_fetchers(monkeypatch, jdk=None, spring=None, maven=None):
    """Install recording fakes for the three fetchers; returns call log."""
    calls = {"jdk": [], "spring": [], "maven": []}

    def fake_jdk(fully_qualified, module="java.base"):
        calls["jdk"].append({"fq": fully_qualified, "module": module})
        if jdk is not None:
            return jdk(fully_qualified, module)
        return {"ok": False, "error": f"HTTP 404 for {fully_qualified} in {module}"}

    def fake_spring(section="index"):
        calls["spring"].append({"section": section})
        if spring is not None:
            return spring(section)
        return {"ok": False, "error": f"HTTP 404 for spring section '{section}'"}

    def fake_maven(group_id, artifact_id, version=None):
        calls["maven"].append(
            {"group": group_id, "artifact": artifact_id, "version": version}
        )
        if maven is not None:
            return maven(group_id, artifact_id, version)
        return {"ok": False, "error": f"artifact not found: {artifact_id}"}

    monkeypatch.setattr(server_mod, "fetch_jdk_class", fake_jdk)
    monkeypatch.setattr(server_mod, "fetch_spring_boot_section", fake_spring)
    monkeypatch.setattr(server_mod, "fetch_maven_artifact", fake_maven)
    return calls


def _ok_jdk(fq: str, module: str, title: str, markdown: str) -> dict:
    package, _, simple = fq.rpartition(".")
    url = f"{JDK_BASE}/{module}/{package.replace('.', '/')}/{simple}.html"
    return {"ok": True, "url": url, "title": title, "markdown": markdown}


# ---------------------------------------------------------------------------
# java_docs — identifier resolution
# ---------------------------------------------------------------------------

def test_spring_identifiers_use_spring_fetcher(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        spring=lambda sec: {
            "ok": True,
            "url": f"https://docs.spring.io/spring-boot/reference/{sec}/index.html",
            "title": f"Spring Boot: {sec}",
            "markdown": "# Spring Boot\n\nBody text.\n",
            "section": sec,
        },
    )
    r1 = server_mod.java_docs("spring")
    assert calls["spring"] == [{"section": "index"}]
    assert r1["type"] == "spring_section"
    assert r1["cached"] is False and r1["truncated"] is False
    assert "docs.spring.io" in r1["url"]

    r2 = server_mod.java_docs("spring:using")
    assert calls["spring"][1] == {"section": "using"}
    assert r2["type"] == "spring_section"
    assert "/using/" in r2["url"]


def test_spring_fetch_failure_returns_error_dict(fake_cache_dir, monkeypatch):
    _patch_fetchers(monkeypatch)  # spring fake returns ok: False
    result = server_mod.java_docs("spring:bogus-section")
    assert "error" in result and "suggestion" in result
    assert "type" not in result


def test_fqn_uses_module_from_index(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def jdk(fq, module="java.base"):
        if fq == "java.util.List" and module == "java.base":
            return _ok_jdk(fq, module, "List Interface", LIST_MD)
        if fq == "java.net.http.HttpClient" and module == "java.net.http":
            return _ok_jdk(fq, module, "HttpClient Class", "# HttpClient\n\ntext\n")
        return {"ok": False, "error": f"HTTP 404 for {fq} in {module}"}

    calls = _patch_fetchers(monkeypatch, jdk=jdk)
    r1 = server_mod.java_docs("java.util.List")
    assert calls["jdk"] == [{"fq": "java.util.List", "module": "java.base"}]
    assert r1["type"] == "jdk_class" and "List" in r1["title"]
    assert r1["url"].endswith("/java.base/java/util/List.html")

    # module java.net.http comes from the fake index, not from a fallback guess
    r2 = server_mod.java_docs("java.net.http.HttpClient")
    assert calls["jdk"][1] == {"fq": "java.net.http.HttpClient", "module": "java.net.http"}
    assert r2["type"] == "jdk_class"


def test_fqn_not_in_index_defaults_to_java_base_then_fallbacks(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)  # no java.sql.Connection entry

    def jdk(fq, module="java.base"):
        if fq == "java.sql.Connection" and module == "java.sql":
            return _ok_jdk(fq, module, "Connection Interface", "# Connection\n\ntext\n")
        return {"ok": False, "error": f"HTTP 404 for {fq} in {module}"}

    calls = _patch_fetchers(monkeypatch, jdk=jdk)
    result = server_mod.java_docs("java.sql.Connection")
    assert result["type"] == "jdk_class"
    modules_tried = [c["module"] for c in calls["jdk"]]
    assert modules_tried[0] == "java.base"  # default before any fallback
    assert modules_tried.index("java.sql") > modules_tried.index("jdk.httpclient")
    assert result["url"].endswith("/java.sql/java/sql/Connection.html")


def test_fqn_all_modules_fail_returns_error_with_suggestion(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(monkeypatch)  # every jdk fetch fails
    result = server_mod.java_docs("com.example.Ghost")
    assert "error" in result and "suggestion" in result
    assert "java_search" in result["suggestion"]
    # java.base + the five fallback modules, each tried exactly once
    assert [c["module"] for c in calls["jdk"]] == [
        "java.base",
        "jdk.httpclient",
        "java.sql",
        "java.desktop",
        "jdk.crypto.ec",
        "jdk.management",
    ]


def test_plain_name_resolved_via_index(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def jdk(fq, module="java.base"):
        if fq == "java.net.http.HttpClient" and module == "java.net.http":
            return _ok_jdk(fq, module, "HttpClient Class", "# HttpClient\n\ntext\n")
        return {"ok": False, "error": f"HTTP 404 for {fq} in {module}"}

    calls = _patch_fetchers(monkeypatch, jdk=jdk)
    result = server_mod.java_docs("HttpClient")
    assert calls["jdk"] == [{"fq": "java.net.http.HttpClient", "module": "java.net.http"}]
    assert result["type"] == "jdk_class"
    assert "note" not in result  # single index entry → no ambiguity note


def test_plain_name_ambiguity_prefers_class_and_notes_alternates(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def jdk(fq, module="java.base"):
        if fq == "java.util.Timer" and module == "java.base":
            return _ok_jdk(fq, module, "Timer Class", TOPIC_MD)
        return {"ok": False, "error": f"HTTP 404 for {fq} in {module}"}

    calls = _patch_fetchers(monkeypatch, jdk=jdk)
    result = server_mod.java_docs("Timer")
    assert calls["jdk"][0] == {"fq": "java.util.Timer", "module": "java.base"}
    assert result["type"] == "jdk_class"
    assert "ambiguous name 'Timer'" in result["note"]
    assert "javax.swing.Timer" in result["note"]


def test_unknown_plain_name_falls_through_to_error(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)  # no SuchClass entry
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.java_docs("TotallyBogusXYZ")
    assert "error" in result and "suggestion" in result
    assert "java_search" in result["suggestion"]
    assert calls["jdk"] == []  # no package to build an FQN from → no fetches


def test_empty_identifier_returns_error(fake_cache_dir, monkeypatch):
    _patch_fetchers(monkeypatch)
    result = server_mod.java_docs("   ")
    assert "error" in result and "suggestion" in result


# ---------------------------------------------------------------------------
# java_docs — topic filter + truncation
# ---------------------------------------------------------------------------

def test_topic_filter_keeps_only_matching_section(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def jdk(fq, module="java.base"):
        if fq == "java.util.Timer" and module == "java.base":
            return _ok_jdk(fq, module, "Timer Class", TOPIC_MD)
        return {"ok": False, "error": "404"}

    _patch_fetchers(monkeypatch, jdk=jdk)
    result = server_mod.java_docs("java.util.Timer", topic="methods")
    assert result["content"].startswith("# Timer Class")
    assert "Starts a timer." in result["content"]  # description kept
    assert "## Methods" in result["content"]
    assert "start()" in result["content"]
    assert "## Fields" not in result["content"]
    assert "note" not in result


def test_topic_filter_case_insensitive_substring(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def jdk(fq, module="java.base"):
        if fq == "java.util.Timer":
            return _ok_jdk(fq, module, "Timer Class", TOPIC_MD)
        return {"ok": False, "error": "404"}

    _patch_fetchers(monkeypatch, jdk=jdk)
    result = server_mod.java_docs("java.util.Timer", topic="FIELD")
    assert "## Fields" in result["content"]
    assert "## Methods" not in result["content"]


def test_topic_without_match_returns_full_content_with_note(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def jdk(fq, module="java.base"):
        if fq == "java.util.Timer":
            return _ok_jdk(fq, module, "Timer Class", TOPIC_MD)
        return {"ok": False, "error": "404"}

    _patch_fetchers(monkeypatch, jdk=jdk)
    result = server_mod.java_docs("java.util.Timer", topic="constructors")
    assert "## Methods" in result["content"] and "## Fields" in result["content"]
    assert "no section matching 'constructors'" in result["note"]
    assert "Methods" in result["note"] and "Fields" in result["note"]


def test_truncation_cuts_at_line_boundary_with_note(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    long_md = "# Big Class\n\n" + "\n".join(f"line {i} " + "x" * 40 for i in range(600))

    def jdk(fq, module="java.base"):
        if fq == "java.util.List":
            return _ok_jdk(fq, module, "Big Class", long_md)
        return {"ok": False, "error": "404"}

    _patch_fetchers(monkeypatch, jdk=jdk)
    result = server_mod.java_docs("java.util.List", max_tokens=100)
    assert result["truncated"] is True
    last_line = result["content"].splitlines()[-1]
    assert last_line.startswith("[truncated: showing ~")
    assert last_line.endswith("estimated tokens]")
    match = re.search(r"showing ~(\d+) of ~(\d+) estimated tokens\]", last_line)
    assert match is not None
    shown, total = int(match.group(1)), int(match.group(2))
    assert shown <= 100 and total > 100
    # cut at a line boundary: no partial "line N xxx..." tail before the note
    body_lines = result["content"].splitlines()[:-1]
    assert all(l.startswith("line ") or l.startswith("#") for l in body_lines if l)


def test_no_truncation_when_within_budget(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def jdk(fq, module="java.base"):
        if fq == "java.util.List":
            return _ok_jdk(fq, module, "List Interface", LIST_MD)
        return {"ok": False, "error": "404"}

    _patch_fetchers(monkeypatch, jdk=jdk)
    result = server_mod.java_docs("java.util.List", max_tokens=8000)
    assert result["truncated"] is False
    assert "[truncated:" not in result["content"]


# ---------------------------------------------------------------------------
# java_docs — caching (real DocCache against a temp dir)
# ---------------------------------------------------------------------------

def test_jdk_doc_cached_on_second_call(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        jdk=lambda fq, module="java.base": (
            _ok_jdk(fq, module, "List Interface", LIST_MD)
            if fq == "java.util.List" and module == "java.base"
            else {"ok": False, "error": "404"}
        ),
    )
    first = server_mod.java_docs("java.util.List")
    second = server_mod.java_docs("java.util.List")
    assert first["cached"] is False and second["cached"] is True
    assert len(calls["jdk"]) == 1
    assert second["content"] == first["content"]


# ---------------------------------------------------------------------------
# java_search
# ---------------------------------------------------------------------------

def test_java_search_shape(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    monkeypatch.setattr(
        server_mod,
        "search",
        lambda q, limit=8, index=None: [
            {
                "name": "HttpClient",
                "package": "java.net.http",
                "module": "java.net.http",
                "kind": "class",
                "url": f"{JDK_BASE}/java.net.http/java/net/http/HttpClient.html",
                "score": 10.0,
            }
        ][: max(0, limit)],
    )
    result = server_mod.java_search("HttpClient", limit=3)
    assert result["query"] == "HttpClient"
    assert result["count"] == 1
    assert result["results"][0]["name"] == "HttpClient"
    assert result["index_stale"] is False


def test_java_search_reports_index_error_note(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, {"built_at": None, "entries": [], "error": "boom"})
    monkeypatch.setattr(server_mod, "search", lambda q, limit=8, index=None: [])
    result = server_mod.java_search("List")
    assert result["count"] == 0 and result["results"] == []
    assert "index unavailable" in result.get("note", "")


# ---------------------------------------------------------------------------
# maven_package
# ---------------------------------------------------------------------------

def test_maven_package_success_and_cache(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        maven=lambda g, a, v=None: {
            "ok": True,
            "group_id": "com.google.guava",
            "artifact_id": "guava",
            "version": "33.4.0-jre",
            "versions": ["33.4.0-jre", "33.3.1-jre", "33.0.0-jre"],
            "description": "Guava core libraries",
            "url": "https://central.sonatype.com/artifact/com.google.guava/guava",
            "javadoc_url": "https://www.javadoc.io/doc/com.google.guava/guava",
        },
    )
    first = server_mod.maven_package(artifact_id="guava")
    assert first["cached"] is False
    assert first["group_id"] == "com.google.guava"
    assert first["version"] == "33.4.0-jre"
    assert isinstance(first["versions"], list) and len(first["versions"]) <= 20
    assert first["url"] and first["javadoc_url"]

    second = server_mod.maven_package(artifact_id="guava")
    assert second["cached"] is True
    assert second["version"] == "33.4.0-jre"
    assert len(calls["maven"]) == 1  # fetcher hit only once


def test_maven_package_versions_capped_at_20(fake_cache_dir, monkeypatch):
    _patch_fetchers(
        monkeypatch,
        maven=lambda g, a, v=None: {
            "ok": True,
            "group_id": "g",
            "artifact_id": "a",
            "version": "1.0",
            "versions": [f"{i}.0" for i in range(35)],
            "url": "u",
            "javadoc_url": None,
        },
    )
    result = server_mod.maven_package(artifact_id="a")
    assert len(result["versions"]) == 20


def test_maven_package_failure_shape_and_no_cache(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        maven=lambda g, a, v=None: {
            "ok": False,
            "error": "artifact not found on Maven Central",
            "suggestion": "No results for 'nope-xyz'. Verify the coordinates.",
        },
    )
    first = server_mod.maven_package(artifact_id="nope-xyz")
    assert first["error"] == "maven lookup failed: artifact not found on Maven Central"
    assert "suggestion" in first
    second = server_mod.maven_package(artifact_id="nope-xyz")
    assert "error" in second  # failures are never cached → fetcher called again
    assert len(calls["maven"]) == 2


def test_maven_package_requires_artifact(fake_cache_dir, monkeypatch):
    _patch_fetchers(monkeypatch)
    result = server_mod.maven_package(artifact_id="  ")
    assert "error" in result and "suggestion" in result


# ---------------------------------------------------------------------------
# java_status
# ---------------------------------------------------------------------------

def test_java_status_all_ok(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    result = server_mod.java_status()
    assert result["server"] == "java-spring-mcp"
    assert result["overall"] == "ok"
    assert set(result["checks"]) == {
        "search_index",
        "cache",
        "docs_oracle_com",
        "docs_spring_io",
        "search_maven_org",
    }
    si = result["checks"]["search_index"]
    assert si["status"] == "ok" and si["entries"] == len(FAKE_INDEX["entries"])
    assert si["stale"] is False and si["built_at"] == FAKE_INDEX["built_at"]
    assert result["checks"]["cache"]["status"] == "ok"
    for probe in ("docs_oracle_com", "docs_spring_io", "search_maven_org"):
        assert result["checks"][probe] == {"status": "ok", "http_status": 200}


def test_java_status_degraded_when_probe_fails(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def probe(url):
        if "maven" in url:
            return {"status": "error", "http_status": None, "error": "timeout"}
        return {"status": "ok", "http_status": 200}

    monkeypatch.setattr(server_mod, "_probe_endpoint", probe)
    result = server_mod.java_status()
    assert result["overall"] == "degraded"
    assert result["checks"]["search_maven_org"]["status"] == "error"


def test_java_status_error_when_index_broken(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, {"built_at": None, "entries": [], "error": "boom"})
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    result = server_mod.java_status()
    assert result["overall"] == "error"
    assert result["checks"]["search_index"]["status"] == "error"
