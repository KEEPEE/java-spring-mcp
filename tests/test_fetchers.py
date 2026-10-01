"""Offline tests for java_spring_mcp.fetchers using real downloaded fixtures.

No network access: everything is parsed from tests/fixtures/, which were
downloaded once from the live sources (see fixture URLs below).
"""

import json
import re
from pathlib import Path

import pytest

from java_spring_mcp.fetchers import (
    fetch_jdk_class,
    parse_jdk_html,
    parse_maven_search_json,
    parse_spring_section_html,
)

FIXTURES = Path(__file__).parent / "fixtures"

JDK_URL = "https://docs.oracle.com/en/java/javase/26/docs/api/java.base/java/util/List.html"
SPRING_URL = "https://docs.spring.io/spring-boot/reference/using/index.html"


def _normalized(md: str) -> str:
    """Collapse all whitespace so assertions are robust to line wrapping."""
    return re.sub(r"\s+", " ", md)


@pytest.fixture(scope="module")
def jdk_result() -> dict:
    html = (FIXTURES / "jdk_list.html").read_text(encoding="utf-8")
    return parse_jdk_html(html, JDK_URL)


@pytest.fixture(scope="module")
def spring_result() -> dict:
    html = (FIXTURES / "spring_using.html").read_text(encoding="utf-8")
    return parse_spring_section_html(html, SPRING_URL)


@pytest.fixture(scope="module")
def maven_docs() -> list[dict]:
    data = json.loads((FIXTURES / "maven_springboot.json").read_text(encoding="utf-8"))
    return parse_maven_search_json(data)


# ---------------------------------------------------------------------------
# JDK javadoc fixture
# ---------------------------------------------------------------------------

def test_jdk_title(jdk_result):
    assert "List" in jdk_result["title"]


def test_jdk_markdown_non_empty(jdk_result):
    assert jdk_result["markdown"].strip()


def test_jdk_markdown_contains_real_description_phrase(jdk_result):
    md = _normalized(jdk_result["markdown"])
    # Real opening sentence of the List javadoc description.
    assert (
        "An ordered collection, where the user has precise control over where in "
        "the list each element is inserted."
    ) in md


def test_jdk_markdown_has_api_sections(jdk_result):
    md = jdk_result["markdown"]
    assert "## Method Summary" in md
    assert "## Method Details" in md
    # A declared member with its signature, as rendered in the summary table.
    assert "`add(int index, E element)`" in md


def test_jdk_markdown_links_are_absolute(jdk_result):
    md = jdk_result["markdown"]
    assert (
        "https://docs.oracle.com/en/java/javase/26/docs/api/java.base/java/util/Collection.html"
        in md
    )


def test_jdk_markdown_has_no_nav_junk(jdk_result):
    md = jdk_result["markdown"]
    # These strings exist only in the javadoc top/sub navigation, which must be stripped.
    assert "Skip navigation links" not in md
    assert "Toggle navigation links" not in md
    assert "Search documentation (type /)" not in md


# ---------------------------------------------------------------------------
# Spring Boot reference fixture
# ---------------------------------------------------------------------------

def test_spring_title(spring_result):
    assert "Spring Boot" in spring_result["title"]


def test_spring_markdown_non_empty(spring_result):
    assert spring_result["markdown"].strip()


def test_spring_markdown_has_multiple_headings(spring_result):
    md = spring_result["markdown"]
    headings = [line for line in md.splitlines() if line.startswith("#")]
    # h1 page title plus the "## Contents" subsection list.
    assert len(headings) >= 2


def test_spring_markdown_contains_real_content(spring_result):
    md = _normalized(spring_result["markdown"])
    assert (
        "This section goes into more detail about how you should use Spring Boot."
        in md
    )


def test_spring_contents_list_links_are_absolute(spring_result):
    md = spring_result["markdown"]
    assert "## Contents" in md
    assert (
        "[Build Systems](https://docs.spring.io/spring-boot/reference/using/build-systems.html)"
        in md
    )


def test_spring_markdown_has_no_nav_junk(spring_result):
    md = spring_result["markdown"]
    # "Why Spring" is a top-navbar dropdown label; "Stack Overflow" is a
    # sidebar footer link - both must be stripped from the article markdown.
    assert "Why Spring" not in md
    assert "Stack Overflow" not in md


# ---------------------------------------------------------------------------
# Maven Central search JSON fixture
# ---------------------------------------------------------------------------

def test_maven_parse_returns_versions(maven_docs):
    assert len(maven_docs) >= 1
    versions = [d["version"] for d in maven_docs]
    assert all(isinstance(v, str) and v for v in versions)


def test_maven_parse_coordinates(maven_docs):
    assert all(d["group_id"] == "org.springframework.boot" for d in maven_docs)
    assert all(d["artifact_id"] == "spring-boot" for d in maven_docs)


def test_maven_versions_newest_first(maven_docs):
    versions = [d["version"] for d in maven_docs]
    # Newest release of spring-boot in the fixture is 3.5.3.
    assert versions[0] == "3.5.3"
    # Ordering must be monotonically non-increasing by publish timestamp.
    stamps = [d["timestamp"] for d in maven_docs]
    assert stamps == sorted(stamps, reverse=True)


def test_maven_parse_handles_missing_data():
    assert parse_maven_search_json({}) == []
    assert parse_maven_search_json({"response": {"docs": None}}) == []
    assert parse_maven_search_json("not a dict") == []


# ---------------------------------------------------------------------------
# Parser/fetcher contracts (still offline)
# ---------------------------------------------------------------------------

def test_parse_jdk_rejects_non_javadoc_html():
    with pytest.raises(ValueError):
        parse_jdk_html("<html><body><p>no javadoc here</p></body></html>", "http://example.com/")


def test_parse_spring_rejects_non_reference_html():
    with pytest.raises(ValueError):
        parse_spring_section_html(
            "<html><body><div class='main'>nothing</div></body></html>", "http://example.com/"
        )


def test_fetch_jdk_class_rejects_invalid_input_without_network():
    result = fetch_jdk_class("NoDotsHere")
    assert result["ok"] is False
    assert "error" in result
