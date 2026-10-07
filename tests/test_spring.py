"""Offline tests for the Spring documentation layer's parsers and rankers.

Nothing here touches the network: every input is a fixture captured from a real
response (see ``tests/fixtures`` and the provenance notes in
``java_spring_mcp.spring``'s module docstring).  These tests cover the parts that
can be wrong on their own — Antora nav parsing, the ranking ladder, Initializr's
HAL quirks, GitHub release-section classification — before the tools that use
them are exercised in ``test_spring_tools.py``.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from java_spring_mcp import spring

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


# ---------------------------------------------------------------------------
# Reference navigation (Antora)
# ---------------------------------------------------------------------------


def test_parse_reference_nav_reads_the_real_nav():
    entries = spring.parse_reference_nav(
        fixture_text("spring_ref_index.html"),
        "https://docs.spring.io/spring-boot/reference/index.html",
    )
    by_slug = {e["slug"]: e for e in entries}

    auto = by_slug["auto-configuration"]
    assert auto["section"] == "using"
    assert auto["depth"] == 3
    # Paths are site-absolute, which is what makes a versioned base swappable.
    assert auto["path"] == "/spring-boot/reference/using/auto-configuration.html"
    assert auto["url"] == "https://docs.spring.io/spring-boot/reference/using/auto-configuration.html"
    assert auto["title"] == "Auto-configuration"

    # Section landing pages are entries too.  Antora renders their href as
    # "index.html", so the slug is "index" and the section name carries the
    # topic: "actuator" is found through section, not slug.
    section_page = next(e for e in entries if e["path"] == "/spring-boot/reference/actuator/index.html")
    assert section_page["slug"] == "index"
    assert section_page["section"] == "actuator"
    assert section_page["depth"] == 2
    assert section_page["title"] == "Production-ready Features"
    assert section_page["parent"] == "Reference"

    # Absolute links in the nav (api/, appendix/, how-to/) are kept as-is.
    absolute = [e for e in entries if e["url"].startswith("https://docs.spring.io/spring-boot/appendix/")]
    assert absolute, "the nav fixture lost the appendix links"


def test_parse_reference_nav_rejects_a_page_that_is_not_docs_spring_io():
    with pytest.raises(ValueError, match="no <aside class=\"nav\">"):
        spring.parse_reference_nav("<html><body><p>hello</p></body></html>", "https://example.com/")


def test_detect_docs_version_reads_the_version_marker():
    assert spring.detect_docs_version(fixture_text("spring_ref_index.html")) == "4.1.1"
    assert spring.detect_docs_version(fixture_text("spring_page_reference_actuator_index.html")) == "4.1.1"
    assert spring.detect_docs_version("<html><body>no version</body></html>") == ""


def test_reference_base_tracks_the_requested_version():
    assert spring.reference_base() == "https://docs.spring.io/spring-boot/reference"
    assert spring.reference_base("4.1.1") == "https://docs.spring.io/spring-boot/4.1.1/reference"
    # A version that is not a plain X.Y[.Z] line raises instead of being pasted
    # into a URL that would 404.
    with pytest.raises(ValueError, match="must look like"):
        spring.reference_base("nope")


# ---------------------------------------------------------------------------
# In-page headings and excerpts
# ---------------------------------------------------------------------------


def test_parse_page_headings_finds_the_jpa_anchor_with_an_excerpt():
    headings = spring.parse_page_headings(
        fixture_text("spring_data_sql.html"),
        "https://docs.spring.io/spring-boot/reference/data/sql.html",
    )
    jpa = next(h for h in headings if h["anchor"] == "data.sql.jpa-and-spring-data")
    assert jpa["level"] == "h2"
    assert jpa["text"] == "JPA and Spring Data JPA"
    # The excerpt is captured at harvest time so a search can quote it without
    # re-reading the page.
    assert jpa["excerpt"].startswith("The Java Persistence API is a standard technology")
    assert len(jpa["excerpt"]) <= 241  # 240 characters plus the ellipsis


def test_heading_excerpt_stops_at_the_next_heading():
    html = (
        "<article class='doc'><h2 id='a'>A</h2><p>First body.</p>"
        "<h3 id='b'>B</h3><p>Second body.</p></article>"
    )
    headings = spring.parse_page_headings(html, "https://docs.spring.io/x")
    assert headings[0]["excerpt"] == "First body."
    assert headings[1]["excerpt"] == "Second body."


def test_page_lede_covers_landing_pages_that_have_no_headings():
    html = fixture_text("spring_page_reference_actuator_index.html")
    assert spring.parse_page_headings(html, "u") == []
    assert spring.page_lede(html).startswith("Spring Boot includes a number of additional features")


def test_excerpt_after_heading_looks_up_a_stored_excerpt():
    html = fixture_text("spring_data_sql.html")
    assert spring.excerpt_after_heading(html, "data.sql.jdbc-template") == ""
    found = spring.excerpt_after_heading(html, "data.sql.jpa-and-spring-data")
    assert found.startswith("The Java Persistence API")


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def nav_entries():
    return spring.parse_reference_nav(
        fixture_text("spring_ref_index.html"),
        "https://docs.spring.io/spring-boot/reference/index.html",
    )


def test_rank_entries_requires_every_token_by_default(nav_entries):
    # No page in the nav is titled after both "data" and "jpa", so the strict
    # pass returns nothing and the search has to read page headings instead.
    assert spring.rank_entries("data jpa", nav_entries) == []

    loose = spring.rank_entries("data jpa", nav_entries, limit=10, require_all=False)
    assert loose
    assert all(hit["matched_tokens"] < 2 for hit in loose)
    # A partial hit is a hint: it is halved, so it can never outrank a real one.
    assert max(hit["score"] for hit in loose) < 3.0

    both = spring.rank_entries("data sql", nav_entries, limit=5)
    assert both and all(hit["matched_tokens"] == 2 for hit in both)


def test_rank_entries_reference_section_page_beats_an_exact_title(nav_entries):
    hits = spring.rank_entries("actuator", nav_entries, limit=20)
    top = hits[0]
    # "/spring-boot/how-to/actuator.html" is titled exactly "Actuator" and still
    # loses to the reference section page, which matches through its section
    # name and gets the reference bonus.  This is the ordering the third-party
    # tool never managed to produce.
    assert top["path"] == "/spring-boot/reference/actuator/index.html"
    assert top["matched_fields"] == ["section"]
    exact_title = next(h for h in hits if h["path"] == "/spring-boot/how-to/actuator.html")
    assert exact_title["matched_on"] == "title"
    assert top["score"] > exact_title["score"]


def test_rank_entries_reference_pages_outrank_howto_pages(nav_entries):
    hits = spring.rank_entries("actuator", nav_entries, limit=30)
    reference = [h for h in hits if spring._is_reference_path(h["path"])]
    how_to = [h for h in hits if "/how-to/" in h["path"]]
    assert reference and how_to
    assert min(h["score"] for h in reference) >= max(h["score"] for h in how_to)


def test_rank_headings_finds_a_heading_no_page_is_titled_after(nav_entries):
    headings = spring.parse_page_headings(
        fixture_text("spring_data_sql.html"),
        "https://docs.spring.io/spring-boot/reference/data/sql.html",
    )
    page = next(e for e in nav_entries if e["slug"] == "sql")
    hits = spring.rank_headings("data jpa", headings, page, limit=5)
    assert hits[0]["anchor"] == "data.sql.jpa-and-spring-data"
    assert hits[0]["score"] > 5.0


def test_candidate_pages_prefers_reference_pages_and_is_capped(nav_entries):
    pages = spring.candidate_pages("data jpa", nav_entries)
    assert 0 < len(pages) <= spring.HEADING_PAGE_LIMIT
    assert any(p["slug"] == "sql" for p in pages)


def test_normalize_splits_hyphenated_terms():
    assert spring._normalize("auto-configuration") == "auto configuration"


# ---------------------------------------------------------------------------
# Guides
# ---------------------------------------------------------------------------


def test_parse_guides_page_data_reads_the_gatsby_payload():
    guides = spring.parse_guides_page_data(fixture_text("spring_guides_page_data.json"))
    assert len(guides) == 8
    first = guides[0]
    assert first["url"].startswith("https://spring.io/guides/")
    assert first["title"]
    assert first["type"] in {"getting-started", "topical", "tutorial"}
    assert first["slug"]


def test_guide_url_for_accepts_slug_forms():
    assert spring.guide_url_for("gs/spring-boot") == "https://spring.io/guides/gs/spring-boot/"
    assert spring.guide_url_for("spring-boot") == "https://spring.io/guides/gs/spring-boot/"
    assert spring.guide_url_for("/guides/gs/spring-boot/") == "https://spring.io/guides/gs/spring-boot/"
    assert spring.guide_url_for("topicals/spring-boot-3") == "https://spring.io/guides/topicals/spring-boot-3/"
    assert spring.guide_url_for("https://spring.io/guides/gs/testing-web/") == (
        "https://spring.io/guides/gs/testing-web/"
    )


def test_parse_guide_html_extracts_the_guide_body_as_markdown():
    parsed = spring.parse_guide_html(
        fixture_text("spring_guide_gs_boot.html"), "https://spring.io/guides/gs/spring-boot/"
    )
    assert parsed["title"] == "Building an Application with Spring Boot"
    assert parsed["markdown"].startswith("# Building an Application with Spring Boot")
    assert "## What You Will build" in parsed["markdown"]
    # The "All guides" breadcrumb button is chrome and is dropped; the phrase
    # still appears inside the guide's own license note, so match the link.
    assert "[All guides]" not in parsed["markdown"]
    assert "All guides are released with an ASLv2 license" in parsed["markdown"]


def test_parse_guide_html_raises_without_a_guide_container():
    with pytest.raises(ValueError, match="no guide container"):
        spring.parse_guide_html("<html><body><p>nope</p></body></html>", "https://spring.io/guides/x/")


# ---------------------------------------------------------------------------
# Initializr
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def initializr():
    return spring.parse_initializr_metadata(fixture_json("initializr_metadata.json"))


def test_parse_initializr_metadata_reports_the_real_versions(initializr):
    assert initializr["boot_versions"]["default"] == "4.1.1.RELEASE"
    # The newest *released* line, not the newest row in the list: Initializr also
    # offers 4.2.0.M2 and two BUILD-SNAPSHOT rows.
    assert initializr["boot_versions"]["latest_released"] == "4.1.1.RELEASE"
    assert "4.2.0.M2" in [v["id"] for v in initializr["boot_versions"]["values"]]
    assert initializr["java_versions"]["default"] == "17"
    assert [g["name"] for g in initializr["dependency_groups"]] == ["Web", "Security", "SQL", "Ops"]
    assert initializr["dependency_count"] == 20


def test_parse_initializr_metadata_survives_a_list_valued_hal_link(initializr):
    # Measured live: `rest-websockets` carries `_links.reference` as a list,
    # which crashed the first parser with 'list' object has no attribute 'get'.
    with_links = [d for d in initializr["dependencies"] if d["reference_url"]]
    assert with_links
    assert all(isinstance(d["reference_url"], str) for d in initializr["dependencies"])


def test_rank_dependencies_matches_catalogue_ids(initializr):
    hits = spring.rank_dependencies("jpa", initializr["dependencies"], limit=5)
    assert hits[0]["id"] == "data-jpa"
    assert hits[0]["name"] == "Spring Data JPA"
    assert "data.sql.jpa-and-spring-data" in hits[0]["reference_url"]

    oauth = spring.rank_dependencies("oauth2 client", initializr["dependencies"], limit=5)
    assert oauth[0]["id"] == "oauth2-client"


def test_plain_boot_version_refuses_milestones():
    assert spring.plain_boot_version("4.1.1.RELEASE") == "4.1.1"
    assert spring.plain_boot_version("4.1.1") == "4.1.1"
    assert spring.plain_boot_version("4.1") == "4.1"
    assert spring.plain_boot_version("4.2.0.M2") is None
    assert spring.plain_boot_version("4.2.0.BUILD-SNAPSHOT") is None
    assert spring.plain_boot_version(None) is None


def test_parse_pom_dependencies_reads_the_generated_pom():
    coordinates = spring.parse_pom_dependencies(fixture_text("springboot_pom_data_jpa.xml"))
    artifacts = [c["artifact_id"] for c in coordinates]
    assert "spring-boot-starter-data-jpa" in artifacts
    assert all(c["group_id"] == "org.springframework.boot" for c in coordinates)
    assert any(c["scope"] == "test" for c in coordinates)


def test_parse_gradle_dependencies_reads_the_generated_build_file():
    coordinates = spring.parse_gradle_dependencies(fixture_text("springboot_build_data_jpa.gradle"))
    artifacts = [c["artifact_id"] for c in coordinates]
    assert "spring-boot-starter-data-jpa" in artifacts
    assert any(c["scope"] == "test" for c in coordinates)


def test_generated_pom_for_web_says_webmvc_not_web():
    # The anti-invention evidence: Initializr's own answer for the `web` id on
    # Spring Boot 4.1 is spring-boot-starter-webmvc.
    coordinates = spring.parse_pom_dependencies(fixture_text("springboot_pom_web.xml"))
    artifacts = [c["artifact_id"] for c in coordinates]
    assert "spring-boot-starter-webmvc" in artifacts
    assert "spring-boot-starter-web" not in artifacts


def test_maven_and_gradle_snippets_are_paste_ready():
    coordinates = [
        {"group_id": "org.springframework.boot", "artifact_id": "spring-boot-starter-data-jpa", "scope": "compile"},
        {"group_id": "org.springframework.boot", "artifact_id": "spring-boot-starter-data-jpa-test", "scope": "test"},
    ]
    maven = spring.maven_snippet(coordinates)
    assert "<artifactId>spring-boot-starter-data-jpa</artifactId>" in maven
    assert "<scope>test</scope>" in maven
    gradle = spring.gradle_snippet(coordinates)
    assert "implementation 'org.springframework.boot:spring-boot-starter-data-jpa'" in gradle
    assert "testImplementation 'org.springframework.boot:spring-boot-starter-data-jpa-test'" in gradle


def test_fetch_initializr_build_files_rejects_the_release_suffix_form():
    # Measured: bootVersion=4.1.1.RELEASE makes /build.gradle answer 500.
    result = spring.fetch_initializr_build_files(["data-jpa"], "4.1.1.RELEASE")
    assert result["ok"] is False
    assert "4.1.1.RELEASE" in result["error"]


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------


def test_strip_emoji_handles_github_shortcodes():
    assert spring._strip_emoji(":warning: Attention Required") == "attention required"
    assert spring._strip_emoji(":lady_beetle: Bug Fixes") == "bug fixes"
    assert spring._strip_emoji("⚠️ Attention Required") == "attention required"


def test_parse_release_classifies_the_measured_section_kinds():
    releases = fixture_json("github_releases.json")
    release = next(r for r in releases if r["tag_name"] == "v4.1.1")
    parsed = spring.parse_release(release)
    kinds = {s["kind"] for s in parsed["sections"]}
    assert "breaking-changes" in kinds  # "## :warning: Attention Required"
    assert "bug-fixes" in kinds
    assert "dependency-upgrades" in kinds
    assert parsed["prerelease"] is False
    assert parsed["published_at"] == "2026-08-20T20:02:36Z"


def test_filter_release_sections_focus():
    releases = fixture_json("github_releases.json")
    parsed = spring.parse_release(next(r for r in releases if r["tag_name"] == "v4.1.1"))
    everything = spring.filter_release_sections(parsed["sections"], "all")
    assert everything["breaking-changes"]
    assert everything["bug-fixes"]
    # Deprecations are a keyword view across sections, not a heading of their own.
    assert everything["deprecations"]
    only = spring.filter_release_sections(parsed["sections"], "breaking-changes")
    assert set(only) == {"breaking-changes"}
    # An unknown focus degrades to "all" instead of returning nothing.
    assert spring.filter_release_sections(parsed["sections"], "nonsense")["bug-fixes"]


def test_parse_release_keeps_only_spring_wiki_links():
    releases = fixture_json("github_releases.json")
    parsed = spring.parse_release(next(r for r in releases if r["tag_name"] == "v4.1.0"))
    assert parsed["wiki_links"] == [
        "https://github.com/spring-projects/spring-boot/wiki/Spring-Boot-4.1-Release-Notes"
    ]


def test_normalize_version_tag_strips_the_v_prefix():
    assert spring.normalize_version_tag("v4.1.1") == "4.1.1"
    assert spring.normalize_version_tag("4.1") == "4.1"


def test_fetch_github_releases_rejects_an_unknown_project():
    result = spring.fetch_github_releases("spring-teapot")
    assert result["ok"] is False
    assert "spring-boot" in result["suggestion"]


# ---------------------------------------------------------------------------
# URL resolution (offline once the index exists)
# ---------------------------------------------------------------------------


def test_is_reference_path_separates_reference_from_api_and_appendix():
    assert spring._is_reference_path("/spring-boot/reference/actuator/index.html")
    assert not spring._is_reference_path("/spring-boot/api/rest/actuator/index.html")
    assert not spring._is_reference_path("/spring-boot/appendix/auto-configuration-classes/index.html")
    assert not spring._is_reference_path("/spring-boot/how-to/actuator.html")


def test_error_helper_shape():
    error = spring._error("boom", "try again")
    assert error == {"ok": False, "error": "boom", "suggestion": "try again"}
    with_extra = spring._error("boom", "try again", extra={"candidates": ["a"]})
    assert with_extra["candidates"] == ["a"]
