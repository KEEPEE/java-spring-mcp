"""Offline tests for the six Spring tools exposed by ``java_spring_mcp.server``.

The transport is replaced at its lowest point — ``fetchers._http_get`` and the
same function bound into ``java_spring_mcp.spring`` — with a recorder that serves
``tests/fixtures``.  Everything above it runs for real: the concept index, the
heading harvest, the ranking, the truncation helpers and ``DocCache`` (a real
SQLite file under ``tmp_path``).  No test here opens a socket.

Fixtures are trimmed copies of real responses; see the provenance notes in
``java_spring_mcp.spring``'s module docstring.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from java_spring_mcp import fetchers as fetchers_mod
from java_spring_mcp import server as server_mod
from java_spring_mcp import spring as spring_mod

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
DOCS = "https://docs.spring.io/spring-boot/"

#: URL -> fixture.  Every body here came from the URL it is keyed by.
DOC_PAGES = {
    DOCS + "reference/index.html": "spring_ref_index.html",
    DOCS + "reference/data/index.html": "spring_page_reference_data_index.html",
    DOCS + "reference/data/sql.html": "spring_data_sql.html",
    DOCS + "reference/data/nosql.html": "spring_page_reference_data_nosql.html",
    DOCS + "reference/web/index.html": "spring_page_reference_web_index.html",
    DOCS + "reference/web/servlet.html": "spring_page_reference_web_servlet.html",
    DOCS + "reference/web/graceful-shutdown.html": "spring_page_reference_web_graceful-shutdown.html",
    DOCS + "reference/actuator/index.html": "spring_page_reference_actuator_index.html",
    DOCS + "reference/actuator/auditing.html": "spring_page_reference_actuator_auditing.html",
    DOCS + "reference/using/auto-configuration.html": "spring_page_reference_using_auto-configuration.html",
    DOCS + "reference/using/running-your-application.html": "spring_page_reference_using_running-your-application.html",
    DOCS + "reference/features/profiles.html": "spring_page_reference_features_profiles.html",
    DOCS + "reference/features/developing-auto-configuration.html": "spring_page_reference_features_developing-auto-configuration.html",
    DOCS + "reference/security/index.html": "spring_page_reference_security_index.html",
    DOCS + "reference/security/oauth2.html": "spring_page_reference_security_oauth2.html",
    DOCS + "reference/using/index.html": "spring_page_reference_using_index.html",
    "https://spring.io/page-data/guides/page-data.json": "spring_guides_page_data.json",
    "https://spring.io/guides/gs/spring-boot/": "spring_guide_gs_boot.html",
    "https://start.spring.io/": "initializr_metadata.json",
    "https://api.github.com/repos/spring-projects/spring-boot/releases?per_page=100": "github_releases.json",
    "https://api.github.com/repos/spring-projects/spring-framework/releases?per_page=100": "github_releases.json",
}


@pytest.fixture
def transport(tmp_path, monkeypatch):
    """Serve fixtures through the real fetch plumbing; return the request log."""
    monkeypatch.setenv("JAVA_SPRING_MCP_CACHE_DIR", str(tmp_path / "cache"))
    requests: list[str] = []

    def fake_get(url, params=None, *, budget_scope=None, budget_limit=6, revalidate=True, headers=None):
        requests.append(url)
        path, _, query = url.partition("?")
        if path.endswith("/pom.xml") or path.endswith("/build.gradle"):
            # Initializr generates these; the fixtures are its real output for
            # the exact dependency set being asked for.
            params = dict(part.split("=", 1) for part in query.split("&"))
            # Initializr's own URLs percent-encode the comma between dependency
            # ids, so decode before using it as a fixture key.
            deps = params.get("dependencies", "").replace("%2C", ",").replace("%2c", ",")
            tag = deps.replace(",", "_").replace("-", "_") or "none"
            name = f"springboot_pom_{tag}.xml" if path.endswith("/pom.xml") else f"springboot_build_{tag}.gradle"
            if not (FIXTURES / name).exists():
                return fetchers_mod._Response(404, "", url, error=f"no build fixture for {deps!r}")
        else:
            name = DOC_PAGES.get(url)
            if name is None:
                # A versioned tree (…/spring-boot/4.1.1/reference/…) serves the
                # same page as the unversioned one, so the same fixture answers.
                versioned = re.sub(
                    r"^https://docs\.spring\.io/spring-boot/\d+\.\d+(?:\.\d+)?/",
                    "https://docs.spring.io/spring-boot/",
                    url,
                )
                name = DOC_PAGES.get(versioned)
            if name is None:
                return fetchers_mod._Response(404, "", url, error=f"HTTP 404 (no fixture for {url})")
        body = (FIXTURES / name).read_text(encoding="utf-8")
        # docs.spring.io sends Last-Modified, spring.io / start.spring.io /
        # api.github.com send an ETag; both are modelled so the revalidation
        # bookkeeping that ``_page_html`` reads back is exercised for real.
        return fetchers_mod._Response(200, body, url, validators={"etag": '"fixture-etag"'})

    monkeypatch.setattr(fetchers_mod, "_http_get", fake_get)
    monkeypatch.setattr(spring_mod, "_http_get", fake_get)
    return requests


def urls_after(requests: list[str], since: int) -> list[str]:
    return requests[since:]


# ---------------------------------------------------------------------------
# spring_search_concepts
# ---------------------------------------------------------------------------

REQUIRED_TOPICS = ("auto-configuration", "profiles", "actuator", "web", "data jpa", "security")


@pytest.mark.parametrize("topic", REQUIRED_TOPICS)
def test_search_answers_the_topics_the_third_party_tool_could_not(transport, topic):
    """The reason this layer exists.

    ``@enokdev/springdocs-mcp``'s ``search_spring_concepts`` answered "Concept
    not found in documentation." for auto-configuration, profiles and actuator.
    Each of these must return at least one hit inside the reference manual.
    """
    result = server_mod.spring_search_concepts(topic, limit=5)
    assert "error" not in result, result
    assert result["hit_count"] >= 1
    reference_hits = [h for h in result["hits"] if "/spring-boot/reference/" in h["url"]]
    assert reference_hits, f"{topic!r} produced no reference-manual hit"
    hit = reference_hits[0]
    assert hit["title"]
    assert hit["score"] > 0
    # Every hit is quotable: either the page was read (excerpt from its body) or
    # the caller is told plainly that it was not.
    assert hit["excerpt_source"] in {"page", "not fetched"}


def test_search_finds_headings_no_page_is_titled_after(transport):
    """`data jpa` has no page named for it; the heading harvest finds the section."""
    result = server_mod.spring_search_concepts("data jpa", limit=4)
    anchors = [h["url"] for h in result["hits"]]
    assert "https://docs.spring.io/spring-boot/reference/data/sql.html#data.sql.jpa-and-spring-data" in anchors
    heading_hit = next(h for h in result["hits"] if h["matched_on"] == "heading")
    assert heading_hit["page_title"] == "SQL Databases"
    assert heading_hit["excerpt"].startswith("The Java Persistence API")
    assert result["pages_read"] >= 1
    assert result["headings_harvested"] >= 1


def test_search_quotes_the_page_it_read(transport):
    result = server_mod.spring_search_concepts("actuator", limit=3)
    top = result["hits"][0]
    assert top["url"] == DOCS + "reference/actuator/index.html"
    assert top["excerpt_source"] == "page"
    # A landing page has no sub-headings, so its own opening paragraph is quoted.
    assert top["excerpt"].startswith("Spring Boot includes a number of additional features")


def test_search_second_call_costs_no_requests(transport):
    first = server_mod.spring_search_concepts("data jpa", limit=8)
    spent_after_first = len(transport)
    second = server_mod.spring_search_concepts("data jpa", limit=8)
    assert urls_after(transport, spent_after_first) == []
    # Not just "some of the same URLs": a warm answer is the same answer.  The
    # hits a cold call produced by reading a page must come back from the
    # heading store, or the second call of a query would be worse than the
    # first one.
    def signature(result):
        return [(h["matched_on"], h["url"], round(h["score"], 2)) for h in result["hits"]]

    assert signature(second) == signature(first)
    assert second["index"]["cached"] is True
    assert second["pages_read"] == 0  # the harvested headings are already stored
    # A warm answer is as quotable as a cold one: the page summary is stored
    # alongside the headings, so a hit on a page that was already read still
    # quotes it instead of returning a bare title.
    quoted_in_cold = {h["url"].split("#")[0] for h in first["hits"] if h["excerpt"]}
    assert quoted_in_cold
    for hit in second["hits"]:
        if hit["url"].split("#")[0] in quoted_in_cold:
            assert hit["excerpt"], f"warm call lost the excerpt for {hit['url']}"
            assert hit["excerpt_source"] == "page"


def test_search_headings_persist_across_related_queries(transport):
    server_mod.spring_search_concepts("data jpa", limit=3)
    spent = len(transport)
    # "connection pool" is a heading of the page "data jpa" already harvested.
    result = server_mod.spring_search_concepts("connection pool", limit=5)
    assert "https://docs.spring.io/spring-boot/reference/data/sql.html#data.sql.datasource.connection-pool" in [
        h["url"] for h in result["hits"]
    ]
    new_pages = [u for u in urls_after(transport, spent) if "/reference/" in u]
    assert new_pages == [], f"re-read a page whose headings were stored: {new_pages}"


def test_search_empty_query_is_an_error_dict(transport):
    result = server_mod.spring_search_concepts("   ")
    assert "error" in result and "suggestion" in result
    assert transport == []  # rejected before anything is fetched


def test_search_reports_the_docs_version_it_read(transport):
    result = server_mod.spring_search_concepts("web", limit=3)
    assert result["version"] == "4.1.1"
    assert result["docs_base"] == DOCS + "reference"


def test_search_limit_is_respected(transport):
    result = server_mod.spring_search_concepts("actuator", limit=2)
    assert result["hit_count"] == 2
    assert len(result["hits"]) == 2


# ---------------------------------------------------------------------------
# spring_reference
# ---------------------------------------------------------------------------

def test_reference_returns_a_section_as_markdown(transport):
    result = server_mod.spring_reference("web")
    assert result["type"] == "spring-reference"
    assert result["url"] == DOCS + "reference/web/index.html"
    assert result["content"].startswith("# Web")
    assert "spring-boot-starter-webmvc" in result["content"]
    assert result["truncated"] is False
    assert result["cached"] is False


def test_reference_second_call_is_a_cache_hit(transport):
    server_mod.spring_reference("web")
    spent = len(transport)
    again = server_mod.spring_reference("web")
    assert urls_after(transport, spent) == []
    assert again["cached"] is True


def test_reference_subsection_resolves_to_a_page(transport):
    result = server_mod.spring_reference("data", "sql")
    assert result["url"] == DOCS + "reference/data/sql.html"
    assert result["content"].startswith("# SQL Databases")
    assert "JPA and Spring Data JPA" in result["content"]
    assert result["identifier"] == "data:sql"


def test_reference_unknown_subsection_filters_the_section_page(transport):
    # "gradually" is a heading on a page, not the name of a page.  The honest
    # answer is the section page, filtered, with a pointer to the tool that can
    # find the page that holds the heading.
    result = server_mod.spring_reference("using", "gradually")
    assert "is not a page of its own" in result["note"]
    assert "spring_search_concepts" in result["note"]
    assert result["url"] == DOCS + "reference/using/index.html"


def test_reference_subsection_accepts_spaced_names(transport):
    """The page is graceful-shutdown.html; the caller wrote "graceful shutdown"."""
    result = server_mod.spring_reference("web", "graceful shutdown")
    assert result["url"] == DOCS + "reference/web/graceful-shutdown.html"
    assert result["content"].startswith("# Graceful Shutdown")
    assert "note" not in result


def test_reference_unknown_section_lists_candidates(transport):
    result = server_mod.spring_reference("teapot")
    assert "error" in result and "suggestion" in result
    assert result["candidates"]
    assert all("/spring-boot/" in c["url"] for c in result["candidates"])


def test_reference_max_tokens_truncates(transport):
    result = server_mod.spring_reference("data", "sql", max_tokens=400)
    assert result["truncated"] is True
    assert len(result["content"]) <= 400 * 4 + 200


def test_reference_version_selects_the_versioned_docs_tree(transport):
    result = server_mod.spring_reference("web", version="4.1.1")
    assert result["url"] == "https://docs.spring.io/spring-boot/4.1.1/reference/web/index.html"
    assert result["version"] == "4.1.1"


def test_reference_bad_version_is_rejected(transport):
    result = server_mod.spring_reference("web", version="not-a-version")
    assert "error" in result and "suggestion" in result


# ---------------------------------------------------------------------------
# spring_guides
# ---------------------------------------------------------------------------

def test_guides_lists_the_catalogue(transport):
    result = server_mod.spring_guides()
    assert result["type"] == "spring-guide-catalogue"
    assert result["url"] == "https://spring.io/page-data/guides/page-data.json"
    assert result["guide_count"] == 8
    guide = result["guides"][0]
    assert guide["url"].startswith("https://spring.io/guides/")
    assert guide["title"]
    assert guide["type"] in {"getting-started", "topical", "tutorial"}


def test_guides_list_is_cached(transport):
    server_mod.spring_guides()
    spent = len(transport)
    again = server_mod.spring_guides()
    assert urls_after(transport, spent) == []
    assert again["cached"] is True


def test_guides_topic_filters_the_catalogue(transport):
    result = server_mod.spring_guides(topic="web")
    assert result["guides"]
    assert all("web" in (g["title"] + " " + g["slug"]).lower() for g in result["guides"])


def test_guides_fetches_one_guide_as_markdown(transport):
    result = server_mod.spring_guides(guide="gs/spring-boot", max_tokens=1200)
    assert result["type"] == "spring-guide"
    assert result["title"] == "Building an Application with Spring Boot"
    assert result["url"] == "https://spring.io/guides/gs/spring-boot/"
    assert result["content"].startswith("# Building an Application with Spring Boot")
    assert "## What You Will build" in result["content"]


def test_guides_accepts_a_bare_slug(transport):
    result = server_mod.spring_guides(guide="spring-boot", max_tokens=400)
    assert result["url"] == "https://spring.io/guides/gs/spring-boot/"


def test_guides_unknown_guide_is_an_error_dict(transport):
    result = server_mod.spring_guides(guide="gs/not-a-guide")
    assert "error" in result and "suggestion" in result


# ---------------------------------------------------------------------------
# spring_initializr
# ---------------------------------------------------------------------------

def test_initializr_options_reports_the_real_versions(transport):
    result = server_mod.spring_initializr("options")
    assert result["source"] == "start.spring.io"
    assert result["boot_version_default"] == "4.1.1.RELEASE"
    assert result["boot_version_latest_released"] == "4.1.1.RELEASE"
    assert result["java_versions"]["default"] == "17"
    assert [g["name"] for g in result["dependency_groups"]] == ["Web", "Security", "SQL", "Ops"]
    assert result["dependency_count"] == 20


def test_initializr_options_is_cached(transport):
    server_mod.spring_initializr("options")
    spent = len(transport)
    again = server_mod.spring_initializr("options")
    assert urls_after(transport, spent) == []
    assert again["cached"] is True


def test_initializr_dependencies_filters_by_text(transport):
    result = server_mod.spring_initializr("dependencies", query="jpa")
    assert result["dependency_count"] >= 1
    top = result["dependencies"][0]
    assert top["id"] == "data-jpa"
    assert top["name"] == "Spring Data JPA"
    assert top["group"] == "SQL"
    assert "data.sql.jpa-and-spring-data" in top["reference_url"]


def test_initializr_dependencies_lists_everything_without_a_query(transport):
    result = server_mod.spring_initializr("dependencies")
    assert result["dependency_count"] == 20
    assert len(result["dependencies"]) == 20


def test_initializr_bad_section_is_an_error_dict(transport):
    result = server_mod.spring_initializr("teapot")
    assert "error" in result and "suggestion" in result
    assert "options" in result["suggestion"] and "dependencies" in result["suggestion"]


# ---------------------------------------------------------------------------
# spring_dependency
# ---------------------------------------------------------------------------

def test_dependency_jpa_returns_the_generated_coordinates(transport):
    result = server_mod.spring_dependency("jpa")
    assert result["type"] == "spring-dependency"
    assert result["boot_version"] == "4.1.1"
    assert result["matches"][0]["id"] == "data-jpa"

    artifacts = [c["artifact_id"] for c in result["coordinates"]]
    assert "spring-boot-starter-data-jpa" in artifacts
    # The coordinates came from Initializr's own pom, not from a guess.
    assert all(c["group_id"] == "org.springframework.boot" for c in result["coordinates"])

    assert "<artifactId>spring-boot-starter-data-jpa</artifactId>" in result["maven_snippet"]
    assert "implementation 'org.springframework.boot:spring-boot-starter-data-jpa'" in result["gradle_snippet"]
    assert result["source"]["pom_url"].startswith("https://start.spring.io/pom.xml?")


def test_dependency_never_invents_an_artifact(transport):
    """`web` maps to spring-boot-starter-webmvc because Initializr says so."""
    result = server_mod.spring_dependency("web")
    artifacts = [c["artifact_id"] for c in result["coordinates"]]
    assert "spring-boot-starter-webmvc" in artifacts
    assert "spring-boot-starter-web" not in artifacts
    assert result["matches"][0]["id"] == "web"


def test_dependency_build_gradle_only(transport):
    result = server_mod.spring_dependency("oauth2 client", build="gradle")
    assert "spring-boot-starter-security-oauth2-client" in result["gradle_snippet"]
    assert result["maven_snippet"] == ""
    first = result["coordinates"][0]["artifact_id"]
    assert first == "spring-boot-starter-security-oauth2-client"


def test_dependency_second_call_is_a_cache_hit(transport):
    server_mod.spring_dependency("jpa")
    spent = len(transport)
    again = server_mod.spring_dependency("jpa")
    assert urls_after(transport, spent) == []
    assert again["source"]["cached"] is True


def test_dependency_unknown_need_returns_groups_not_a_guess(transport):
    result = server_mod.spring_dependency("flux capacitor")
    assert "error" in result and "suggestion" in result
    assert "catalogue" in result["suggestion"]
    assert result["dependency_groups"]
    assert "coordinates" not in result


def test_dependency_explicit_boot_version_is_used(transport):
    result = server_mod.spring_dependency("jpa", boot_version="4.1.1")
    assert result["boot_version"] == "4.1.1"
    assert "bootVersion=4.1.1" in result["source"]["pom_url"]
    # Initializr's own build endpoints reject "4.1.1.RELEASE", so the tool
    # normalizes it away instead of forwarding a version that would 500.
    normalized = server_mod.spring_dependency("jpa", boot_version="4.1.1.RELEASE")
    assert "bootVersion=4.1.1&" in normalized["source"]["pom_url"] + "&"
    assert "4.1.1.RELEASE" not in normalized["source"]["pom_url"]


# ---------------------------------------------------------------------------
# spring_versions
# ---------------------------------------------------------------------------

def test_versions_latest_skips_prereleases(transport):
    """The fixture is in GitHub's real order: v4.2.0-M2 first, then v4.1.1."""
    result = server_mod.spring_versions()
    assert result["type"] == "spring-release-notes"
    assert result["tag"] == "v4.1.1"
    assert result["prerelease"] is False
    assert result["latest_stable"] == "v4.1.1"
    assert result["latest_listed"] == "v4.2.0-M2"
    assert result["published_at"] == "2026-08-20T20:02:36Z"


def test_versions_renders_sections_in_reading_order(transport):
    result = server_mod.spring_versions(max_tokens=1200)
    content = result["content"]
    assert "## Breaking Changes" in content
    assert content.index("## Breaking Changes") < content.index("## Bug Fixes")
    assert content.index("## Bug Fixes") < content.index("## Dependency Upgrades")


def test_versions_focus_breaking_changes(transport):
    result = server_mod.spring_versions(version="4.1.1", focus="breaking-changes")
    assert result["focus"] == "breaking-changes"
    assert result["counts"] == {"breaking-changes": 1}
    assert "## Breaking Changes" in result["content"]
    assert "## Bug Fixes" not in result["content"]


def test_versions_focus_new_features(transport):
    result = server_mod.spring_versions(version="4.1.0", focus="new-features")
    assert result["tag"] == "v4.1.0"
    assert result["counts"]["new-features"] == 2
    assert result["wiki_links"] == [
        "https://github.com/spring-projects/spring-boot/wiki/Spring-Boot-4.1-Release-Notes"
    ]


def test_versions_second_call_is_a_cache_hit(transport):
    server_mod.spring_versions()
    spent = len(transport)
    again = server_mod.spring_versions()
    # GitHub's unauthenticated quota is 60/hour per IP, so a warm call must not
    # spend another request.
    assert urls_after(transport, spent) == []
    assert again["cached"] is True


def test_versions_unknown_version_lists_available_tags(transport):
    result = server_mod.spring_versions(version="9.9")
    assert "error" in result and "suggestion" in result
    assert "v4.1.1" in result["available_tags"]


def test_versions_unknown_project(transport):
    result = server_mod.spring_versions(project="spring-teapot")
    assert "error" in result and "suggestion" in result
    assert "spring-boot" in result["suggestion"]


def test_versions_bad_focus_is_an_error_dict(transport):
    result = server_mod.spring_versions(focus="nope")
    assert "error" in result and "suggestion" in result


# ---------------------------------------------------------------------------
# Registration and budget
# ---------------------------------------------------------------------------

SPRING_TOOLS = (
    "spring_search_concepts",
    "spring_reference",
    "spring_guides",
    "spring_initializr",
    "spring_dependency",
    "spring_versions",
)


@pytest.mark.asyncio
async def test_spring_tools_are_registered_on_the_server():
    tools = await server_mod.mcp.list_tools()
    names = {t.name for t in tools}
    assert set(SPRING_TOOLS) <= names
    # The four tools this layer must not disturb.
    assert {"java_docs", "java_search", "java_status", "maven_package", "health_check"} <= names


def test_search_stays_inside_the_per_call_budget(transport):
    """One index build plus at most HEADING_PAGE_LIMIT pages = 5 of 6 units."""
    result = server_mod.spring_search_concepts("data jpa", limit=5)
    assert "error" not in result
    assert len(transport) <= fetchers_mod.FETCH_BUDGET_LIMIT
    assert result["pages_read"] <= spring_mod.HEADING_PAGE_LIMIT
