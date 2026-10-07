# java-spring-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI coding agents **live JDK javadoc, Spring Boot reference documentation and Maven Central metadata** instead of whatever the model happens to remember. Pages are scraped from the official sites at request time, cached locally, and returned as clean markdown — so generated Java and Spring code targets real, current APIs rather than deprecated or invented ones.

- **JDK classes** — scraped live from [docs.oracle.com](https://docs.oracle.com/en/java/javase/26/docs/api/) (all 41 modules: `java.base`, `java.sql`, `java.desktop`, `java.net.http`, …), ~4,700 classes with their module recorded, so a bare `HttpClient` resolves to `java.net.http` and not to a guess
- **Spring Boot reference** — all 11 sections of the official [Spring Boot reference](https://docs.spring.io/spring-boot/reference/) (`using`, `features`, `actuator`, `security`, `testing`, `web`, `data`, `io`, `messaging`, `packaging`, `index`)
- **Maven Central** — artifact metadata from the official [search API](https://search.maven.org) plus a javadoc.io link
- **Local search index** over every JDK class plus the Spring sections, rebuilt at most every 7 days, with a stale fallback when the network fails
- **SQLite TTL cache** so repeated lookups are instant
- **Politeness layer** — robots.txt (RFC 9309), per-host throttle incl. `Crawl-delay`, `Retry-After`, conditional GET and request budgets ([details](#caching--politeness))

## The five tools

| Tool | What it does |
|---|---|
| `java_docs` | Resolve one identifier (`java.util.List`, `HttpClient`, `spring:using`) to a single documentation page as markdown. |
| `java_search` | Ranked name search over the local index of all JDK classes and Spring Boot reference sections. |
| `maven_package` | Maven Central artifact metadata: newest matching version, the version list, the central.sonatype.com page and a javadoc.io link. |
| `java_status` | Real health check: index size/age, cache stats, live probes of docs.oracle.com, docs.spring.io and search.maven.org, and politeness counters. |
| `health_check` | Server liveness and version. |

Every tool returns a plain dict. Failures come back as `{"error": …, "suggestion": …}` — a bad lookup never raises into the MCP layer, and no tool delegates to another tool.

## Requirements

- Python 3.10 or newer (developed and tested on 3.12)
- [`uv`](https://docs.astral.sh/uv/) for the one-command install below (`uvx` ships with it)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`); this package does not support the mcp 2.x rename of `FastMCP`

## Install & run

### One command — no checkout, no token

```bash
uvx --from git+https://github.com/KEEPEE/java-spring-mcp.git java-spring-docs
```

This builds the package in an isolated environment and starts the stdio MCP server. It prints nothing on purpose: stdout is the protocol channel. Stop it with `Ctrl-C`.

After the repository is updated, force `uv` to re-resolve the commit:

```bash
uvx --refresh --from git+https://github.com/KEEPEE/java-spring-mcp.git java-spring-docs
```

### Local checkout — fastest startup, editable while developing

```bash
git clone https://github.com/KEEPEE/java-spring-mcp.git
cd java-spring-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/java-spring-docs        # stdio MCP server
```

## MCP client configuration

### Generic stdio client (Claude Desktop, Cursor, Cline, …)

```json
{
  "mcpServers": {
    "java-spring": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/KEEPEE/java-spring-mcp.git",
        "java-spring-docs"
      ]
    }
  }
}
```

With an explicit cache directory (any `env` you set is passed straight through to the server):

```json
{
  "mcpServers": {
    "java-spring": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/KEEPEE/java-spring-mcp.git",
        "java-spring-docs"
      ],
      "env": {
        "JAVA_SPRING_MCP_CACHE_DIR": "/tmp/java-spring-cache"
      }
    }
  }
}
```

### DeepSeek Harness (`cordis`-style plugin list)

```yaml
- id: mcp-java-spring
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: java-spring
    transport: stdio
    command: uvx
    args:
      [
        '--from',
        'git+https://github.com/KEEPEE/java-spring-mcp.git',
        'java-spring-docs'
      ]
```

To skip the build at client start, point `command` at the console script of a checkout instead — `command: /path/to/java-spring-mcp/.venv/bin/java-spring-docs` with `args: []`.

## Tools reference

### `java_docs(identifier, topic=None, max_tokens=8000)`

Resolves an identifier to one documentation page. Identifier forms, tried in this order:

| Form | Meaning |
|---|---|
| `spring`, `spring:using` | Spring Boot reference section (`spring` alone = the index section) |
| `java.util.List`, `java.net.http.HttpClient` | fully-qualified class; the module is read from the search index, falling back to `java.base` plus a fixed module list |
| `HttpClient` | exact-name lookup in the local index (a class beats an interface on ambiguity), then fetch |

`topic` keeps only the section whose heading contains it (`methods`, `method summary`, `fields`, `constructors`, …) plus the page title and description; if nothing matches, the full page is returned with a `note` listing the available headings. `max_tokens` is a rough budget (1 token ≈ 4 characters): the markdown is cut at a line boundary and `truncated` is set.

Returns `{"type", "identifier", "url", "title", "content", "truncated", "cached"}`, plus an optional `note`. `type` is `jdk_class` or `spring_section`.

```jsonc
// java_docs({ "identifier": "java.net.http.HttpClient", "max_tokens": 250 })
// a bare name would also work: "HttpClient" resolves to this same module
{
  "type": "jdk_class",
  "identifier": "java.net.http.HttpClient",
  "url": "https://docs.oracle.com/en/java/javase/26/docs/api/java.net.http/java/net/http/HttpClient.html",
  "content": "# Class HttpClient\n\n```java\npublic abstract class HttpClient extends Object implements AutoCloseable\n```\n\nAn HTTP Client.\n\nAn `HttpClient` can be used to send requests and retrieve their responses … [truncated: showing ~243 of ~9883 estimated tokens]",
  "truncated": true,
  "cached": false,
  "title": "Class HttpClient"
}
```

```jsonc
// java_docs({ "identifier": "spring:using", "max_tokens": 250 })
{
  "type": "spring_section",
  "identifier": "spring:using",
  "url": "https://docs.spring.io/spring-boot/reference/using/index.html",
  "content": "# Developing with Spring Boot\n\nThis section goes into more detail about how you should use Spring Boot. … ## Contents\n\n- [Build Systems](…)\n- [Structuring Your Code](…) … [truncated: showing ~226 of ~429 estimated tokens]",
  "truncated": true,
  "cached": false,
  "title": "Developing with Spring Boot"
}
```

A `topic` that matches nothing tells you what the page does have:

```jsonc
// java_docs({ "identifier": "java.util.List", "topic": "nonexistent-section", "max_tokens": 120 })
{
  "type": "jdk_class",
  "identifier": "java.util.List",
  "url": "https://docs.oracle.com/en/java/javase/26/docs/api/java.base/java/util/List.html",
  "content": "# Interface List<E>\n\n```java\npublic interface List<E> extends SequencedCollection<E>\n``` … [truncated: showing ~113 of ~1040 estimated tokens]",
  "truncated": true,
  "cached": true,
  "title": "Interface List<E>",
  "note": "no section matching 'nonexistent-section'; available headings: Interface List<E>, Unmodifiable Lists, Method Summary, Methods declared in interface Collection, Methods declared in interface Iterable, …"
}
```

A name that exists nowhere is reported honestly — Oracle answers a wrong class with a redirect, not a 404, so the request budget is what stops the search (see [budgets](#caching--politeness)):

```jsonc
// java_docs({ "identifier": "com.example.NopeXYZ" })
{
  "error": "could not fetch javadoc for 'com.example.NopeXYZ': request budget exhausted for scope 'tool:java_docs'",
  "suggestion": "the per-call request budget (6) ran out while guessing modules; try java_search('com.example.NopeXYZ') to find the right package/module"
}
```

### `java_search(query, limit=8)`

Ranks the local index by name similarity (exact > prefix > substring > fuzzy). Returns `{"query", "count", "results": [{name, package, module, kind, url, score}], "index_stale"}`. `kind` is `class`, `interface`, `enum`, `record`, `annotation` or `spring_section`.

```jsonc
// java_search({ "query": "http client", "limit": 3 })
{
  "query": "http client",
  "count": 3,
  "results": [
    { "name": "HttpClient",         "package": "java.net.http", "module": "java.net.http", "kind": "class",     "url": "https://docs.oracle.com/en/java/javase/26/docs/api/java.net.http/java/net/http/HttpClient.html",         "score": 14.0 },
    { "name": "HttpClient.Builder", "package": "java.net.http", "module": "java.net.http", "kind": "interface", "url": "https://docs.oracle.com/en/java/javase/26/docs/api/java.net.http/java/net/http/HttpClient.Builder.html", "score": 14.0 },
    { "name": "HttpClient.Redirect","package": "java.net.http", "module": "java.net.http", "kind": "enum",      "url": "https://docs.oracle.com/en/java/javase/26/docs/api/java.net.http/java/net/http/HttpClient.Redirect.html",  "score": 14.0 }
  ],
  "index_stale": false
}
```

Spring sections are ranked in the same list, so one search covers both halves of the index:

```jsonc
// java_search({ "query": "spring using", "limit": 3 })
{
  "query": "spring using",
  "count": 3,
  "results": [
    { "name": "Spring Boot: Using", "package": "using",      "module": "spring-boot", "kind": "spring_section", "url": "https://docs.spring.io/spring-boot/reference/using/index.html",                    "score": 14.0 },
    { "name": "Spring",             "package": "javax.swing","module": "java.desktop","kind": "class",          "url": "https://docs.oracle.com/en/java/javase/26/docs/api/java.desktop/javax/swing/Spring.html",     "score": 12.909 },
    { "name": "SpringLayout",       "package": "javax.swing","module": "java.desktop","kind": "class",          "url": "https://docs.oracle.com/en/java/javase/26/docs/api/java.desktop/javax/swing/SpringLayout.html", "score": 9.882 }
  ],
  "index_stale": false
}
```

### `maven_package(artifact_id, group_id=None, version=None, max_tokens=6000)`

Maven Central metadata. `artifact_id` is the artifact to look up; `group_id` pins the group when several projects share an artifact name; `version` selects one release from the result list. Returns `{"group_id", "artifact_id", "version", "versions", "url", "javadoc_url", "cached"}` — `versions` is the newest 20 coordinates, `url` the central.sonatype.com page and `javadoc_url` the javadoc.io page. `max_tokens` is reserved for future javadoc content fetching and currently has no effect.

```jsonc
// maven_package({ "artifact_id": "guava", "group_id": "com.google.guava" })
{
  "group_id": "com.google.guava",
  "artifact_id": "guava",
  "version": "33.4.8-jre",
  "versions": ["33.4.8-jre", "33.4.8-android", "33.4.7-jre", "33.4.7-android", "33.4.6-jre", "… 20 entries in total"],
  "url": "https://central.sonatype.com/artifact/com.google.guava/guava",
  "javadoc_url": "https://www.javadoc.io/doc/com.google.guava/guava",
  "cached": false
}
```

Without `group_id` the search API ranks by relevance, so a bare `spring-boot` legitimately resolves to whichever project Maven Central scores highest — pass the group when you mean a specific one:

```jsonc
// maven_package({ "artifact_id": "spring-boot" })
{ "group_id": "org.apache.camel.springboot", "artifact_id": "spring-boot", "version": "4.20.0", "…": "…" }
```

An unknown coordinate is an error dict, not an empty result:

```jsonc
// maven_package({ "artifact_id": "nope-not-a-real-artifact-xyz" })
{
  "error": "maven lookup failed: artifact not found on Maven Central",
  "suggestion": "No results for 'nope-not-a-real-artifact-xyz'. Verify the coordinates, or retry without a group id: maven_package('nope-not-a-real-artifact-xyz')"
}
```

### `java_status()`

Probes docs.oracle.com, docs.spring.io and search.maven.org with a light GET (10 s timeout) and reports the index and cache state. `overall` is `ok`, `degraded` or `error`. The `politeness` block is diagnostics only and never changes `overall`.

```jsonc
// java_status()
{
  "server": "java-spring-mcp",
  "version": "0.2.0",
  "checks": {
    "search_index":    { "status": "ok", "entries": 4716, "built_at": "2026-10-07T06:52:16.422162+00:00", "stale": false },
    "cache":           { "status": "ok", "entries": 9, "expired": 0 },
    "docs_oracle_com": { "status": "ok", "http_status": 200 },
    "docs_spring_io":  { "status": "ok", "http_status": 200 },
    "search_maven_org":{ "status": "ok", "http_status": 200 }
  },
  "overall": "ok",
  "politeness": {
    "status": "ok", "disabled": false,
    "requests": 15, "robots_requests": 3, "robots_rows": 3, "robots_fetches": 3, "robots_cache_hits": 13,
    "blocked_by_robots": 0, "throttle_waits": 9, "throttle_sleep_s": 3.338,
    "host_delays": { "docs.oracle.com": 0.0, "docs.spring.io": 0.0, "search.maven.org": 0.0 },
    "budgets": { "tool:java_docs": [6, 6], "tool:maven_package": [1, 6] },
    "budget_denied": 1, "conditional": 0, "conditional_skipped": 0, "revalidated_304": 0,
    "redirect_hops": 3, "retries_429": 0, "retries_transport": 0, "stalls": 0, "errors": 0
  }
}
```

### `health_check()`

`{"status": "ok", "server": "java-spring-mcp", "version": "0.2.0"}` — no network, no cache; safe as a liveness probe.

## Caching & politeness

### Cache

Everything lives in one directory: `~/.cache/java-spring-mcp` by default, overridable with `JAVA_SPRING_MCP_CACHE_DIR`.

| File | Contents | TTL |
|---|---|---|
| `cache.db` | fetched pages (parsed result + raw body + `ETag` / `Last-Modified`), the search index, Maven metadata | JDK docs 7 days, Spring sections 7 days, index 7 days, Maven 1 day |
| `robots.db` | the politeness layer's robots.txt cache | 7 days per host |

Delete the files to force a full refresh. A cache problem is never a tool failure: if the directory is unwritable the server simply runs without a cache and says so in `java_status().checks.cache`.

### Politeness

Every outbound request — `fetch_*`, the index build and the `java_status` probes — goes through one small internal module, [`src/java_spring_mcp/politeness.py`](src/java_spring_mcp/politeness.py) (stdlib + `httpx`, no extra dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it ignores wildcards and would report rules such as `Disallow: /*/sp_common/*.htm` as allowed. `docs.oracle.com` publishes over 5,700 rules in a 158 KB file, and among them the older JDK lines — `/en/java/javase/12/` … `/16/` and `/18/` … `/20/`, plus `/javase/6/`, `/7/`, `/9/`, `/10/`, `/1.5.0/` — are disallowed. This server fetches `javase/26`; if an older URL ever reached the layer the answer is `{"ok": false, "error": "blocked by robots.txt for docs.oracle.com — Disallow: /en/java/javase/12/ matches …"}` with **no request made**, not a silent fetch. The javadoc line is a single constant, `fetchers.JDK_API_BASE`, pinned to `javase/26`; the disallow list stops at `/20/`, so `javase/17` and every line from `/21/` up are open to crawlers — repointing that constant at the Java 21 LTS javadoc is the one change that stays robots-clean. Files are cached 7 days per host, including negative results (404 / 403 / 5xx), so a host costs one robots request per week.
- **Per-host throttle, including `Crawl-delay`.** Requests to one host are spaced 0.35–0.9 s apart, or by the site's own `Crawl-delay` when it declares one — `docs.spring.io` declares `crawl-delay: 1` and therefore gets at least one second between requests. A cold run touches three hosts and pays one robots fetch plus one page fetch each, which is exactly where that delay is felt — that is the point (measured: 3 robots fetches + 3 page fetches, 1.56 s spent waiting, `crawl_delay_applied: 1`).
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request is retried once. A response that stalls past the read timeout escalates the delay instead of being retried blindly.
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached result, so refreshing an expired entry costs a `304` instead of a full download. Oracle's javadoc pages send an `ETag` (no `Last-Modified`), so revalidation works there. **`search.maven.org` sends neither `ETag` nor `Last-Modified`** — verified against live response headers — so a conditional GET is not possible for Maven lookups: the fetcher stores no validators for that host and the layer counts the attempt under `conditional_skipped` rather than sending a request that could never yield a `304`.
- **Request budgets.** One tool call may make at most **6 requests in total**, and one budget unit is one request on the wire: the robots fetch, every retry and every redirect hop all pay. That matters here because Oracle answers a wrong class name with a `302` to its landing page rather than a `404`, so nothing in the status line stops the module-guessing loop — the budget does. Measured live: a cold legitimate `java_docs` costs 2–3 units, a cold typo spends all 6 and stops with the error dict above. A standalone index build gets its own `index:docs.oracle.com` budget of 6 and, if it runs out, returns a **partial** index instead of raising.
- **Host allowlist.** Only `docs.oracle.com`, `docs.spring.io` and `search.maven.org` can be contacted, so a malformed identifier or a surprising redirect cannot turn a docs lookup into a request to somebody else's site.
- **One transport, no `curl` fallback.** Earlier versions shelled out to `curl` when `httpx` failed. That second transport is gone and stays gone: it bypassed robots.txt, the throttle, the budget and the stored validators, so a fallback request was an *unpolite* request; it doubled the worst case to roughly two minutes (`--max-time 90` on top of a 20 s `httpx` timeout); and the premise it was written for — that some edges throttle Python's TLS fingerprint — did not reproduce under measurement. The layer's own transport retry, backoff and 10 s stall detection cover the real failure mode in about a fifth of the time, inside the same budget and robots accounting.

The layer never raises and never changes a tool's return shape. `java_status` reports its counters under the top-level `politeness` key.

### Opt-out

```bash
export JAVA_SPRING_MCP_POLITENESS_DISABLED=1
```

This single switch turns off robots.txt, the throttle, retries, conditional GET and the host allowlist at once. **Use it at your own risk:** you take over responsibility for respecting each site's crawling rules, and you are far more likely to be rate-limited or blocked. There is no partial opt-out.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/python -m pytest -q              # 167 offline tests; fixtures in tests/fixtures/
.venv/bin/python scripts/politeness_smoke.py /tmp/java-spring-smoke-cache
.venv/bin/python scripts/e2e_mcp_test.py
```

- `pytest` is fully offline: HTTP is simulated with `httpx.MockTransport` and time/jitter are injected, so the suite is deterministic and runs in seconds. `tests/fixtures/` holds real captured pages (the JDK `List` javadoc, the Spring `using` section, Maven search JSON, the full ~2 MB all-classes index).
- `scripts/politeness_smoke.py <cache-dir>` is a **live measurement**, not a test: it drives the real tools against the real docs sites and prints what the politeness layer did (requests, robots, throttle, conditional GET, budgets).
- `scripts/e2e_mcp_test.py` spawns the installed `java-spring-docs` console script and speaks newline-delimited JSON-RPC to it (`initialize` → `tools/list` → every tool → a negative case → `health_check`). Exit code 0 means every check passed. Set `JAVA_SPRING_MCP_E2E_CMD` to run a different server command.

## Troubleshooting

**The first `java_docs` call takes half a minute.** That is the cold search-index build: the Oracle all-classes page plus a robots fetch, spaced by the per-host throttle, followed by the page you actually asked for. It happens once every 7 days; later calls hit the cached index. Delete `cache.db` and you pay for it again.

**`request budget exhausted for scope 'tool:java_docs'`.** One tool call hit its cap of 6 requests. Oracle redirects a wrong class name to its landing page instead of returning 404, so a name that is not in the index burns the budget on module guesses before the tool gives up. `java_search` first — it is offline and free — and pass the fully-qualified name. `java_status().politeness.budgets` shows how much each scope spent.

**`"partial": true`, or fewer search results than expected.** An index build ran out of its budget and stopped early, returning a partial index with a `partial_reason`. A partial index is **not cached**, so the next run rebuilds it. `java_search` still works — it just knows fewer classes — and `java_status().checks.search_index.entries` tells you how many it has (a complete JDK 26 index is ~4,700 entries).

**`"stale": true` / `index_stale: true`.** The rebuild failed (offline, DNS failure, 5xx) and the server fell back to the older cached index instead of failing the lookup.

**Nothing is cached and `overall` is `degraded`.** Read `java_status().checks.cache.error` — an unwritable `JAVA_SPRING_MCP_CACHE_DIR` (read-only mount, missing permission) is the usual cause. Tools keep working without a cache; they are just slower and noisier on the network.

**A lookup returns `blocked by robots.txt`.** The site's rules disallow that path for this user agent, and the request was not sent. For Oracle this normally means an older JDK javadoc line: `/en/java/javase/12/`–`/16/` and `/18/`–`/20/` are closed to crawlers. Fetch the page yourself, or accept the consequences of the opt-out switch above.

**`maven_package` returns a group you did not expect.** Without `group_id` the Maven search API ranks by relevance, and a common artifact name can belong to several projects. Pass `group_id` to pin the coordinates.

## License & attribution

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Michal Gaspierik.

The **design** of the politeness layer was inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0): a TTL-cached robots store, the wildcard rule translation, and a per-domain rate limiter with escalating backoff. The implementation is a clean-room rewrite in this project's own synchronous stdlib-plus-`httpx` style — no line was transcribed, translated or mechanically adapted from Crawl4AI, and Crawl4AI is not a dependency of this package. Three defects of the original design are fixed (robots `fetched_at` refresh, negative-result caching, `Crawl-delay` support — the last one matters concretely here, because `docs.spring.io` declares `crawl-delay: 1`). The GPL-3.0 part of Crawl4AI — its vendored `html2text` fork — is deliberately excluded: no code, data or dependency from that tree is used or shipped here. The full statement is in [NOTICE](NOTICE).

Same clean-room architecture as [flutter-mcp](https://github.com/KEEPEE/flutter-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures and an offline test suite.
