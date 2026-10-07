"""Live smoke run for the Spring documentation layer.

Not a test — a measurement.  It drives the six Spring tools against the real
docs.spring.io / spring.io / start.spring.io / api.github.com and prints what
each call actually returned plus what the politeness layer spent, cold cache
first and warm cache second, so the report can quote numbers instead of claims.

Usage: .venv/bin/python scripts/live_spring_smoke.py <cache-dir>
"""

from __future__ import annotations

import json
import os
import sys
import time

CACHE_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/java-spring-live-cache"
os.environ["JAVA_SPRING_MCP_CACHE_DIR"] = CACHE_DIR
os.makedirs(CACHE_DIR, exist_ok=True)

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from java_spring_mcp import fetchers, server  # noqa: E402

INTERESTING = (
    "requests",
    "robots_fetches",
    "blocked_by_robots",
    "throttle_waits",
    "throttle_sleep_s",
    "crawl_delay_applied",
    "conditional",
    "revalidated_304",
    "retries_429",
    "retries_transport",
    "stalls",
    "budget_denied",
    "errors",
)


def snap() -> dict:
    s = fetchers.get_politeness().stats()
    out = {k: s.get(k) for k in INTERESTING}
    out["budgets"] = s.get("budgets")
    return out


def brief(result: dict) -> str:
    """Compact, human-readable digest of one tool result."""
    if not isinstance(result, dict):
        return json.dumps(result)[:200]
    if result.get("error"):
        return f"ERROR: {str(result['error'])[:160]}"
    parts: list[str] = []
    for key in ("type", "version", "boot_version_latest_released", "hit_count", "count",
                "guide_count", "dependency_count", "tag", "name", "published_at",
                "latest_stable", "latest_listed", "pages_read", "headings_harvested",
                "cached", "truncated", "note"):
        if key in result and result[key] not in (None, ""):
            parts.append(f"{key}={result[key]}")
    for key in ("hits", "guides", "dependencies", "matches", "coordinates", "results"):
        items = result.get(key)
        if isinstance(items, list):
            parts.append(f"{key}[{len(items)}]=" + json.dumps(
                [{k: v for k, v in item.items() if k in ("title", "url", "id", "name", "group", "score", "matched_on", "excerpt")}
                 for item in items[:4]],
                ensure_ascii=False)[:1400])
    for key in ("maven_snippet", "gradle_snippet", "content", "url"):
        value = result.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key}={value[:700]!r}")
    return "\n      ".join(parts)


def run(label: str, fn) -> dict:
    before = snap()
    t0 = time.time()
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{exc.__class__.__name__}: {exc}"}
    after = snap()
    delta = {
        k: (after[k] or 0) - (before.get(k) or 0)
        for k in INTERESTING
        if isinstance(after.get(k), (int, float)) and isinstance(before.get(k), (int, float))
    }
    print(f"\n=== {label}  ({time.time() - t0:.2f} s)")
    print(f"    politeness delta: {json.dumps({k: v for k, v in delta.items() if v})}")
    print(f"    result  : {brief(result if isinstance(result, dict) else {})[:2600]}")
    return result


def main() -> int:
    print(f"cache dir: {CACHE_DIR}")
    print(f"robots db: {fetchers.default_robots_db_path()}")
    print(f"UA       : {fetchers.USER_AGENT}")
    print(f"timeouts : {fetchers.TIMEOUTS}  budget/call: {fetchers.FETCH_BUDGET_LIMIT}")

    run("spring_search_concepts('auto-configuration')  [cold]",
        lambda: server.spring_search_concepts("auto-configuration", limit=5))
    run("spring_search_concepts('auto-configuration')  [warm]",
        lambda: server.spring_search_concepts("auto-configuration", limit=5))
    run("spring_search_concepts('profiles')", lambda: server.spring_search_concepts("profiles", limit=5))
    run("spring_search_concepts('actuator')", lambda: server.spring_search_concepts("actuator", limit=5))
    run("spring_search_concepts('web')", lambda: server.spring_search_concepts("web", limit=5))
    run("spring_search_concepts('data jpa')  [heading harvest]",
        lambda: server.spring_search_concepts("data jpa", limit=5))
    run("spring_search_concepts('data jpa')  [headings cached]",
        lambda: server.spring_search_concepts("data jpa", limit=5))
    run("spring_search_concepts('security')", lambda: server.spring_search_concepts("security", limit=5))

    run("spring_reference('web')", lambda: server.spring_reference("web", max_tokens=1200))
    run("spring_reference('web')  [warm]", lambda: server.spring_reference("web", max_tokens=1200))
    run("spring_reference('data', 'sql')", lambda: server.spring_reference("data", "sql", max_tokens=1200))
    run("spring_reference('using', 'auto-configuration')",
        lambda: server.spring_reference("using", "auto-configuration", max_tokens=1200))
    run("spring_reference('web', 'profiles')  [topic fallback]",
        lambda: server.spring_reference("web", "profiles", max_tokens=1200))
    run("spring_reference('nope')  [error path]", lambda: server.spring_reference("nope"))

    run("spring_guides()  [catalogue]", lambda: server.spring_guides(max_tokens=1500))
    run("spring_guides()  [warm]", lambda: server.spring_guides(max_tokens=1500))
    run("spring_guides(topic='data')", lambda: server.spring_guides(topic="data", max_tokens=900))
    run("spring_guides(guide='gs/spring-boot')",
        lambda: server.spring_guides(guide="gs/spring-boot", max_tokens=900))

    run("spring_initializr('options')", lambda: server.spring_initializr("options"))
    run("spring_initializr('options')  [warm]", lambda: server.spring_initializr("options"))
    run("spring_initializr('dependencies', query='jpa')",
        lambda: server.spring_initializr("dependencies", query="jpa", max_tokens=1200))

    run("spring_dependency('jpa')", lambda: server.spring_dependency("jpa"))
    run("spring_dependency('jpa')  [warm]", lambda: server.spring_dependency("jpa"))
    run("spring_dependency('oauth2 client', build='gradle')",
        lambda: server.spring_dependency("oauth2 client", build="gradle"))
    run("spring_dependency('flux capacitor')  [error path]",
        lambda: server.spring_dependency("flux capacitor"))

    run("spring_versions()  [latest]", lambda: server.spring_versions(max_tokens=1500))
    run("spring_versions()  [warm]", lambda: server.spring_versions(max_tokens=1500))
    run("spring_versions(version='4.1')", lambda: server.spring_versions(version="4.1", max_tokens=1200))
    run("spring_versions(project='spring-framework')",
        lambda: server.spring_versions(project="spring-framework", max_tokens=900))

    print("\n=== unchanged tools still work")
    run("java_docs('spring:using')", lambda: server.java_docs("spring:using", max_tokens=600))
    run("java_docs('java.util.List')", lambda: server.java_docs("java.util.List", max_tokens=400))
    run("java_search('spring auto')", lambda: server.java_search("spring auto", limit=3))
    run("maven_package('spring-boot')", lambda: server.maven_package("spring-boot"))
    status = server.java_status()
    print(f"    java_status overall={status.get('overall')} checks={sorted(status.get('checks', {}))}")
    print(f"    politeness totals: {json.dumps({k: fetchers.get_politeness().stats().get(k) for k in INTERESTING})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
