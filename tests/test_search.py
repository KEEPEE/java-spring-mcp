"""Offline tests for java_spring_mcp.search (parser, ranking, cache logic).

The parser runs against the real downloaded fixture
``tests/fixtures/jdk_allclasses.html``; the ranking and load_index tests use
hand-built fake indexes and monkeypatched builds, so nothing here touches the
network.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from java_spring_mcp import search as search_mod
from java_spring_mcp.search import (
    SPRING_SECTIONS,
    load_index,
    parse_allclasses_html,
    search,
)

FIXTURE = Path(__file__).parent / "fixtures" / "jdk_allclasses.html"


# ---------------------------------------------------------------------------
# Parser tests on the real fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def parsed_entries() -> list[dict]:
    html = FIXTURE.read_text(encoding="utf-8")
    return parse_allclasses_html(html)


def test_parser_entry_count(parsed_entries):
    assert len(parsed_entries) >= 3000


def test_list_entry_fields(parsed_entries):
    # Note: java.awt.List also exists and sorts first; pick by package.
    lists = [e for e in parsed_entries if e["name"] == "List" and e["package"] == "java.util"]
    assert lists, "java.util.List missing from index"
    entry = lists[0]
    assert entry["package"] == "java.util"
    assert entry["module"] == "java.base"
    assert entry["kind"] == "interface"
    assert entry["url"].startswith("https://")
    assert entry["url"].endswith("java.base/java/util/List.html")


def test_non_base_module_recorded(parsed_entries):
    """Module must be recorded per entry, not assumed to be java.base."""
    by_name = {e["name"]: e for e in parsed_entries}

    # HttpClient: the task's example class. In JDK 26 it lives in the
    # java.net.http module (it was jdk.httpclient in older releases) — either
    # way, it is NOT java.base and the URL must carry its real module.
    hc = by_name.get("HttpClient")
    assert hc is not None, "java.net.http.HttpClient missing from index"
    assert hc["module"] != "java.base"
    assert hc["module"] == "java.net.http"
    assert hc["package"] == "java.net.http"
    assert hc["url"].endswith(f"{hc['module']}/java/net/http/HttpClient.html")

    # java.sql entries: every one of them must carry module java.sql.
    sql = [e for e in parsed_entries if e["package"] == "java.sql"]
    assert len(sql) >= 10, "expected a healthy number of java.sql entries"
    assert all(e["module"] == "java.sql" for e in sql)


def test_every_entry_has_module_matching_url(parsed_entries):
    """The recorded module must agree with the URL's first path segment."""
    for entry in parsed_entries:
        url_path = entry["url"].split("docs/api/", 1)[1]
        assert url_path.startswith(f"{entry['module']}/"), entry
        assert entry["kind"] in {"class", "interface", "enum", "record", "annotation"}


def test_spring_section_entries():
    spring = search_mod._spring_section_entries()
    assert len(spring) == 11
    assert [e["package"] for e in spring] == SPRING_SECTIONS
    assert all(e["kind"] == "spring_section" and e["module"] == "spring-boot" for e in spring)

    by_pkg = {e["package"]: e for e in spring}
    assert by_pkg["index"]["url"] == "https://docs.spring.io/spring-boot/reference/index.html"
    assert by_pkg["using"]["url"] == "https://docs.spring.io/spring-boot/reference/using/index.html"
    assert by_pkg["using"]["name"] == "Spring Boot: Using"
    assert all(e["name"].startswith("Spring Boot: ") for e in spring)


# ---------------------------------------------------------------------------
# Ranking tests on a hand-built fake index (no network, no cache)
# ---------------------------------------------------------------------------

def _fake_index() -> dict:
    return {
        "built_at": "2026-01-01T00:00:00+00:00",
        "entries": [
            {"name": "Zebra", "package": "zoo", "module": "m", "kind": "class", "url": "u-z"},
            {"name": "Lisp", "package": "lisp", "module": "m", "kind": "class", "url": "u-l"},
            {"name": "AbstractList", "package": "java.util", "module": "java.base",
             "kind": "class", "url": "u-al"},
            # starts with the query token (the startswith tier)
            {"name": "ListItem", "package": "java.util", "module": "java.base",
             "kind": "class", "url": "u-li"},
            {"name": "List", "package": "java.util", "module": "java.base",
             "kind": "interface", "url": "u-list"},
        ],
    }


def test_ranking_exact_startswith_substring_fuzzy():
    results = search("list", index=_fake_index())
    names = [r["name"] for r in results]
    # exact > startswith > substring > fuzzy; Zebra (ratio < 0.3) dropped.
    assert names == ["List", "ListItem", "AbstractList", "Lisp"]
    scores = [r["score"] for r in results]
    assert all(a > b for a, b in zip(scores, scores[1:]))
    assert all("score" in r for r in results)
    # original index entries must not be mutated by the search
    assert "score" not in _fake_index()["entries"][0]


def test_ranking_is_case_insensitive():
    results = search("LIST", index=_fake_index())
    assert [r["name"] for r in results][0] == "List"


def test_limit_respected():
    results = search("list", limit=2, index=_fake_index())
    assert len(results) == 2
    assert [r["name"] for r in results] == ["List", "ListItem"]
    assert len(search("list", limit=0, index=_fake_index())) == 0


def test_empty_or_none_query_returns_empty():
    assert search("", index=_fake_index()) == []
    assert search(None, index=_fake_index()) == []
    assert search("   ", index=_fake_index()) == []


def test_multi_token_query_requires_all_tokens():
    fake = _fake_index()
    fake["entries"].append(
        {"name": "Spring Boot: Using", "package": "using", "module": "spring-boot",
         "kind": "spring_section", "url": "u-spring"}
    )
    results = search("spring using", index=fake)
    assert [r["name"] for r in results] == ["Spring Boot: Using"]

    # A token that matches nothing (not even fuzzily) excludes everything.
    assert search("list qzxwv", index=_fake_index()) == []


# ---------------------------------------------------------------------------
# load_index cache behaviour (monkeypatched build, tmp cache dir)
# ---------------------------------------------------------------------------

def _canned() -> dict:
    return {
        "built_at": "2026-01-01T00:00:00+00:00",
        "entries": [
            {"name": "List", "package": "java.util", "module": "java.base",
             "kind": "interface", "url": "u-list"},
        ],
    }


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Point DocCache at a throwaway directory and stub out build_index."""
    monkeypatch.setenv("JAVA_SPRING_MCP_CACHE_DIR", str(tmp_path / "cache"))
    calls = {"count": 0}

    def fake_build():
        calls["count"] += 1
        return _canned()

    monkeypatch.setattr(search_mod, "build_index", fake_build)
    return calls


def test_load_index_fresh_write_then_cache_hit(isolated_cache):
    first = load_index()
    assert first == _canned()
    assert first.get("stale") is not True
    assert isolated_cache["count"] == 1

    # Second call must be served from the cache: no rebuild, same content.
    second = load_index()
    assert second == _canned()
    assert isolated_cache["count"] == 1

    # force_refresh bypasses the cache and rewrites it.
    third = load_index(force_refresh=True)
    assert third == _canned()
    assert isolated_cache["count"] == 2


def test_load_index_stale_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVA_SPRING_MCP_CACHE_DIR", str(tmp_path / "cache"))
    state = {"fail": False}

    def fake_build():
        if state["fail"]:
            raise RuntimeError("network down")
        return _canned()

    monkeypatch.setattr(search_mod, "build_index", fake_build)

    # First build succeeds; use a tiny TTL so the entry expires immediately.
    fresh = load_index(max_age_seconds=0.05)
    assert fresh.get("stale") is not True
    time.sleep(0.06)

    # Rebuild now fails -> the expired cached copy must come back as stale.
    state["fail"] = True
    stale = load_index()
    assert stale.get("stale") is True
    assert stale["entries"] == _canned()["entries"]
    assert stale["built_at"] == _canned()["built_at"]


def test_load_index_build_fails_with_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVA_SPRING_MCP_CACHE_DIR", str(tmp_path / "cache"))

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(search_mod, "build_index", boom)
    result = load_index()  # must not raise
    assert result["entries"] == []
    assert result["built_at"] is None
    assert "boom" in result["error"]


def test_search_uses_index_param_without_network():
    """search(index=...) must never reach for the cache or network."""
    results = search("list", index=_fake_index())
    assert results and results[0]["name"] == "List"
