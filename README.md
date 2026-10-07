# java-spring-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI coding agents **live JDK javadoc, Spring Boot reference documentation and Maven Central metadata** instead of whatever the model happens to remember. Pages are scraped from the official sites at request time, cached locally, and returned as clean markdown — so generated Java and Spring code targets real, current APIs rather than deprecated or invented ones.

- **JDK classes** — scraped live from [docs.oracle.com](https://docs.oracle.com/en/java/javase/26/docs/api/) (all 41 modules: `java.base`, `java.sql`, `java.desktop`, `java.net.http`, …), ~4,700 classes with their module recorded, so a bare `HttpClient` resolves to `java.net.http` and not to a guess
- **Spring Boot documentation** — a first-class layer over the official [Spring Boot reference](https://docs.spring.io/spring-boot/reference/) (all 11 sections: `using`, `features`, `actuator`, `security`, `testing`, `web`, `data`, `io`, `messaging`, `packaging`, `index`), the [Getting Started guides](https://spring.io/guides), [Spring Initializr](https://start.spring.io) and the [release notes](https://github.com/spring-projects/spring-boot/releases). It searches section titles **and** the headings inside each page, which is what a topic query such as `data jpa` actually needs.
- **Maven Central** — artifact metadata from the official [search API](https://search.maven.org) plus a javadoc.io link
- **Local search index** over every JDK class plus the Spring sections, rebuilt at most every 7 days, with a stale fallback when the network fails
- **SQLite TTL cache** so repeated lookups are instant
- **Politeness layer** — robots.txt (RFC 9309), per-host throttle incl. `Crawl-delay`, `Retry-After`, conditional GET and request budgets ([details](#caching--politeness))

## The eleven tools

Java tools:

| Tool | What it does |
|---|---|
| `java_docs` | Resolve one identifier (`java.util.List`, `HttpClient`, `spring:using`) to a single documentation page as markdown. |
| `java_search` | Ranked name search over the local index of all JDK classes and Spring Boot reference sections. |
| `maven_package` | Maven Central artifact metadata: newest matching version, the version list, the central.sonatype.com page and a javadoc.io link. |
| `java_status` | Real health check: index size/age, cache stats, live probes of docs.oracle.com, docs.spring.io and search.maven.org, and politeness counters. |
| `health_check` | Server liveness and version. |

Spring tools (added for the Spring documentation layer — see [Replacing @enokdev/springdocs-mcp](#replacing-enokdevspringdocs-mcp)):

| Tool | What it does |
|---|---|
| `spring_search_concepts` | Ranked hits over the reference manual's **nav titles and in-page headings**, each hit with a quoted excerpt. Answers `auto-configuration`, `profiles`, `actuator`, `web`, `data jpa`, `security`. |
| `spring_reference` | One reference section or subsection as clean markdown, with `topic`-style heading filtering and a `version` selector. |
| `spring_guides` | List the spring.io guides catalogue, or fetch one guide as markdown. |
| `spring_initializr` | Spring Initializr metadata: the Boot and Java versions it offers, project types, and the dependency catalogue (filterable). |
| `spring_dependency` | A need ("jpa", "oauth2 client") → the starter coordinates **Spring Initializr itself generates**, with paste-ready Maven and Gradle blocks. |
| `spring_versions` | Release / migration notes for a Spring project, grouped into breaking changes, new features, deprecations and the rest. |

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

## Spring tools reference

These six tools are the Spring documentation layer. They read four hosts — `docs.spring.io` (reference manual), `spring.io` (guides), `start.spring.io` (Initializr) and `api.github.com` (release notes) — and every one of them is budgeted, throttled and cached like the Java tools.

### `spring_search_concepts(query, version=None, limit=8, max_tokens=4000)`

The question a Spring agent actually asks is by topic, not by page: *"how do profiles work?"*, *"what does the actuator give me?"*, *"JPA with Spring Boot?"*. This tool ranks the reference manual's **navigation titles** (296 pages, read from the section index) **and the headings inside the pages themselves**, and quotes the text under each hit.

Ranking is the same ladder as `java_search` (exact word > prefix > substring, every query word must match), weighted by field (`title` > `slug` > `section` > `parent`) with a bonus for reference pages over how-to pages. When the titles cannot answer the query, the tool fetches up to `HEADING_PAGE_LIMIT` (4) candidate pages, harvests their `h2`–`h4` headings plus a short excerpt for each, and adds those as hits. Harvested headings are cached, so the same query a second time costs **zero requests**.

Returns `{"query", "hit_count", "hits", "index", "version", "docs_base", "pages_read", "headings_harvested", "truncated"}`. Each hit is `{title, url, section, version, score, matched_on, excerpt, excerpt_source, nav_depth, is_reference_page}` (+ `heading_level` and `page_title` for a heading hit). `matched_on` is `title`, `path` or `heading`.

```jsonc
// spring_search_concepts({ "query": "auto-configuration", "max_tokens": 400 })   // live, cold: 3 requests
{
  "query": "auto-configuration",
  "hit_count": 5,
  "hits": [
    { "title": "Auto-configuration", "url": "https://docs.spring.io/spring-boot/reference/using/auto-configuration.html", "section": "using", "version": "4.1.1", "score": 6.9, "matched_on": "title", "excerpt": "Auto-configuration is non-invasive. At any point, you can start to define your own configuration to replace specific parts of the auto-configuration. For example, if you add your own DataSource bean, the default embedded database support…", "excerpt_source": "page", "nav_depth": 3, "is_reference_page": true },
    { "title": "Creating Your Own Auto-configuration", "url": "https://docs.spring.io/spring-boot/reference/features/developing-auto-configuration.html", "section": "features", "version": "4.1.1", "score": 6.9, "matched_on": "title", "excerpt": "Classes that implement auto-configuration are annotated with @AutoConfiguration . This annotation itself is meta-annotated with @Configuration …", "excerpt_source": "page", "nav_depth": 3, "is_reference_page": true },
    { "title": "Auto-configuration Packages", "url": "https://docs.spring.io/spring-boot/reference/using/auto-configuration.html#using.auto-configuration.packages", "section": "using", "version": "4.1.1", "score": 6.9, "matched_on": "heading", "excerpt": "Auto-configuration packages are the packages that various auto-configured features look in by default when scanning for things such as entities and Spring Data repositories. …", "excerpt_source": "page", "nav_depth": 3, "is_reference_page": true, "heading_level": "h2", "page_title": "Auto-configuration" }
  ],
  "index": { "cached": false },
  "version": "4.1.1",
  "docs_base": "https://docs.spring.io/spring-boot/reference",
  "pages_read": 2,
  "headings_harvested": 21,
  "truncated": false
}
```

Measured live for the six topics that the third-party Spring MCP server could not answer: `auto-configuration` 5 hits (2 pages read, 21 headings harvested), `profiles` 4, `actuator` 5, `web` 5, `data jpa` 5, `security` 5 — and the identical second call of each returns the same hits with `pages_read: 0` and no requests at all (0.01 s).

`data jpa` is the case that proves the heading pass is needed: no page is called "data jpa", so the answer comes from a heading — `JPA and Spring Data JPA` at `reference/data/sql.html#data.sql.jpa-and-spring-data`.

### `spring_reference(section, subsection=None, version=None, max_tokens=8000)`

One reference page as clean markdown, with the same `topic` filtering as `java_docs`. `section` is a reference section (`web`, `data`, `using`, `actuator`, …); `subsection` is a page inside it. Hyphens and spaces are the same thing, so `("web", "graceful shutdown")` resolves to `reference/web/graceful-shutdown.html`.

`version` selects the docs tree: `None` → `https://docs.spring.io/spring-boot/reference`, `"4.1"` or `"4.1.1"` → `https://docs.spring.io/spring-boot/4.1.1/reference`. A value that is not `major.minor[.patch]` is rejected rather than guessed at.

Returns the same payload as `java_docs` (`type: "spring-reference"`) plus `version`, `headings` (the page's own outline) and `alternatives` when several pages matched.

```jsonc
// spring_reference({ "section": "data", "subsection": "sql", "max_tokens": 200 })   // live
{
  "type": "spring-reference",
  "identifier": "data:sql",
  "url": "https://docs.spring.io/spring-boot/reference/data/sql.html",
  "title": "SQL Databases",
  "content": "# SQL Databases\n\nThe [Spring Framework](https://spring.io/projects/spring-framework) provides extensive support for work… [truncated: showing ~127 of ~11614 estimated tokens]",
  "truncated": true,
  "cached": false,
  "version": "4.1.1",
  "headings": ["h2 Configure a DataSource", "h3 Embedded Database Support", "h3 Connection to a Production Database", "h2 JPA and Spring Data JPA", "…"]
}
```

When the subsection names a heading rather than a page, the tool says so instead of pretending:

```jsonc
// spring_reference({ "section": "web", "subsection": "profiles" })   // live
{
  "…": "…",
  "url": "https://docs.spring.io/spring-boot/reference/features/profiles.html",
  "note": "'profiles' is not a page under 'web'; it lives at /spring-boot/reference/features/profiles.html (matched by page name)"
}
```

An unknown section lists what does exist instead of returning nothing:

```jsonc
// spring_reference({ "section": "nope" })   // live
{
  "error": "unknown Spring documentation section: no Spring Boot documentation section named 'nope'",
  "suggestion": "spring_search_concepts('<topic>') lists the sections that exist",
  "candidates": [ { "title": "Developing with Spring Boot", "url": "https://docs.spring.io/spring-boot/reference/using/index.html" }, "… 12 section landing pages …" ]
}
```

### `spring_guides(guide=None, topic=None, max_tokens=8000)`

No arguments (or `topic=`) lists the [guides catalogue](https://spring.io/guides); `guide=` fetches one guide as markdown. The catalogue is read from `https://spring.io/page-data/guides/page-data.json`, because `https://spring.io/guides` itself is a client-rendered page: fetched over HTTP it answers `308 → /guides/` and then contains **zero** links. If page-data ever stops working, the tool falls back to `https://spring.io/sitemap-index.xml` (allowed by spring.io's robots.txt) and says so in a `note`, because a sitemap gives URLs only and titles have to be derived from the slug. `guide_count` is the number of guides actually returned, so it drops with `max_tokens` (68 at 8000, 58 at 4000, 22 at 1500).

```jsonc
// spring_guides({})   // live: 68 guides, 1 request (cold); 0 requests warm
{
  "type": "spring-guide-catalogue",
  "source": "page-data",
  "url": "https://spring.io/page-data/guides/page-data.json",
  "guide_count": 68,
  "guides": [
    { "title": "Messaging with Redis", "url": "https://spring.io/guides/gs/messaging-redis/", "slug": "messaging-redis", "type": "getting-started", "category": ["Spring Data", "Redis"], "description": "Learn how to use Redis as a message broker." },
    "…"
  ],
  "truncated": true,
  "cached": false
}
```

`guide=` accepts a slug (`spring-boot`), a kind+slug (`gs/spring-boot`, `topicals/spring-boot-3`), a path (`/guides/gs/spring-boot/`) or a full URL — all resolve to the same canonical guide URL. The markdown keeps the guide body and drops the site chrome (nav, footer, social links, "report an issue").

### `spring_initializr(section="options", query=None, max_tokens=6000)`

Spring Initializr metadata, read from `https://start.spring.io/` with `Accept: application/vnd.initializr.v2.1+json`. This is the only place that says which Boot versions a project can actually be built with today.

```jsonc
// spring_initializr({ "section": "options" })   // live
{
  "type": "initializr-options",
  "source": "start.spring.io",
  "url": "https://start.spring.io/",
  "boot_version_default": "4.1.1.RELEASE",
  "boot_version_latest_released": "4.1.1.RELEASE",
  "boot_versions": {
    "default": "4.1.1.RELEASE",
    "latest_released": "4.1.1.RELEASE",
    "values": [
      { "id": "4.2.0.BUILD-SNAPSHOT", "name": "4.2.0 (SNAPSHOT)" },
      { "id": "4.2.0.M2", "name": "4.2.0 (M2)" },
      { "id": "4.1.2.BUILD-SNAPSHOT", "name": "4.1.2 (SNAPSHOT)" },
      { "id": "4.1.1.RELEASE", "name": "4.1.1" },
      "…"
    ]
  },
  "java_versions": { "default": "17", "values": [ { "id": "27", "name": "27" }, { "id": "25", "name": "25" }, { "id": "21", "name": "21" }, { "id": "17", "name": "17" } ] },
  "project_types": { "default": "gradle-project", "values": [ { "id": "gradle-project", "name": "Gradle - Groovy" }, "…" ] },
  "languages": { "default": "java", "values": [ { "id": "java", "name": "Java" }, { "id": "kotlin", "name": "Kotlin" }, "…" ] },
  "packagings": { "default": "jar", "values": [ { "id": "jar", "name": "Jar" }, { "id": "war", "name": "War" } ] },
  "defaults": { "groupId": "com.example", "artifactId": "demo", "version": "0.0.1-SNAPSHOT", "packageName": "com.example.demo" },
  "dependency_groups": [ { "name": "Developer Tools", "count": 7 }, { "name": "Web", "count": 18 }, { "name": "SQL", "count": 19 }, "…" ],
  "dependency_count": 204,
  "cached": false
}
```

`section="dependencies"` returns the catalogue (204 entries), optionally filtered: `spring_initializr("dependencies", query="jpa")` → `data-jpa` / *Spring Data JPA* / group `SQL`, with the `reference_url` pointing at the exact docs anchor for it.

### `spring_dependency(need, build="both", boot_version=None, max_tokens=4000)`

Turns a need into coordinates. It never invents an artifact: the ids it recommends come from the Initializr catalogue, and the coordinates it prints are the ones **Initializr's own build endpoints generate** for those ids (`https://start.spring.io/pom.xml?type=maven-build&dependencies=…&bootVersion=…` and the `/build.gradle` twin).

That distinction matters. On Boot 4.1 the catalogue id `web` generates `spring-boot-starter-webmvc` — not the `spring-boot-starter-web` that a model would write from memory. Asking for `spring-boot-starter-web` because the need was "web" would be a plausible, wrong dependency.

```jsonc
// spring_dependency({ "need": "jpa" })   // live: 2 requests (pom + gradle)
{
  "type": "spring-dependency",
  "need": "jpa",
  "boot_version": "4.1.1",
  "matches": [ { "id": "data-jpa", "name": "Spring Data JPA", "group": "SQL", "score": 3.0 } ],
  "coordinates": [
    { "group_id": "org.springframework.boot", "artifact_id": "spring-boot-starter-data-jpa", "scope": "compile" },
    { "group_id": "org.springframework.boot", "artifact_id": "spring-boot-starter-data-jpa-test", "scope": "test" }
  ],
  "source": { "pom_url": "https://start.spring.io/pom.xml?type=maven-build&dependencies=data-jpa&bootVersion=4.1.1", "gradle_url": "…", "cached": false },
  "maven_snippet": "<dependencies>\n    <dependency>\n        <groupId>org.springframework.boot</groupId>\n        <artifactId>spring-boot-starter-data-jpa</artifactId>\n    </dependency>\n    …\n</dependencies>",
  "gradle_snippet": "dependencies {\n    implementation 'org.springframework.boot:spring-boot-starter-data-jpa'\n    testImplementation 'org.springframework.boot:spring-boot-starter-data-jpa-test'\n    testRuntimeOnly 'org.junit.platform:junit-platform-launcher'\n}",
  "truncated": false,
  "note": "coordinates are the ones Spring Initializr itself generates for these dependency ids, not names derived from the ids"
}
```

`boot_version` is normalized before it reaches Initializr: `4.1.1.RELEASE` becomes `4.1.1`, because `bootVersion=4.1.1.RELEASE` makes `/build.gradle` fail with `Bom '…spring-boot-dependencies:4.1.1.RELEASE' could not be resolved` (measured: HTTP 500). A need that matches nothing returns the catalogue's groups, not a guess:

```jsonc
// spring_dependency({ "need": "flux capacitor" })   // live
{
  "error": "no Spring Initializr dependency matches 'flux capacitor'",
  "suggestion": "spring_initializr('dependencies') lists every id in the catalogue; this tool only recommends artifacts that catalogue offers",
  "dependency_groups": [ { "name": "Web", "count": 12 }, "…" ]
}
```

### `spring_versions(project="spring-boot", version=None, focus="all", max_tokens=6000)`

Release notes from the GitHub releases API (`api.github.com`), parsed into sections and classified: `new-features`, `breaking-changes`, `deprecations`, `bug-fixes`, `dependency-upgrades`, `documentation`, `other`. GitHub spells its section icons as `## :warning: Attention Required`, so the classifier strips the emoji markers before deciding — without that step every section lands in `other`. `focus=` returns only the requested class (`deprecations` is a keyword view across all sections). `version` accepts `4.1`, `4.1.1` or `v4.1.1`; the patch is optional and the nearest match in the release list is used.

```jsonc
// spring_versions({})   // live
{
  "type": "spring-release-notes",
  "project": "spring-boot",
  "repo": "spring-projects/spring-boot",
  "tag": "v4.1.1",
  "prerelease": false,
  "published_at": "2026-08-20T20:02:36Z",
  "latest_stable": "v4.1.1",          // newest non-prerelease in the list
  "latest_listed": "v4.2.0-M2",       // newest entry, prereleases included
  "focus": "all",
  "counts": { "new-features": 0, "breaking-changes": 1, "deprecations": 4, "bug-fixes": 33, "dependency-upgrades": 57, "documentation": 21, "other": 0 },
  "section_headings": [ ":warning: Attention Required", ":lady_beetle: Bug Fixes", ":notebook_with_decorative_cover: Documentation", ":hammer: Dependency Upgrades", ":heart: Contributors" ],
  "content": "# v4.1.1\n\n## Breaking Changes\n- Spring Boot's Gradle plugin no longer automatically configures gRPC when the Protobuf plugin is applied. … [#50822](…)\n\n## Bug Fixes\n- Kafka consumer-specific security protocol is not taken into account [#51369](…) …",
  "truncated": true,
  "cached": false,
  "url": "https://github.com/spring-projects/spring-boot/releases/tag/v4.1.1"
}
```

A version that does not exist lists the tags that do:

```jsonc
// spring_versions({ "version": "9.9" })   // live
{
  "error": "no spring-projects/spring-boot release matches version '9.9'",
  "suggestion": "spring_versions() with no version lists the newest one",
  "available_tags": [ "v4.2.0-M2", "v4.2.0-M1", "v4.1.1", "v4.0.8", "v3.5.16", "v4.1.0", "…" ]
}
```

`project=` covers `spring-boot`, `spring-framework`, `spring-security`, `spring-data-jpa`, `spring-batch`, `spring-ai` (live: `spring_versions(project="spring-framework")` → `v7.0.9`, published 2026-08-20).

## Which Spring Boot version is current (and why two tools disagree)

The Spring layer had to answer "what is the latest Spring Boot?" from live data instead of from memory, and the two sources disagree:

| Source | Says | Why |
|---|---|---|
| `start.spring.io` (Initializr metadata) | `bootVersion.default = 4.1.1.RELEASE`, latest released **4.1.1**; 4.2.0 is offered only as `M2` / `BUILD-SNAPSHOT`; **no 3.x is offered at all** | it is the version list the project generator will actually build with |
| GitHub releases (`spring-projects/spring-boot`) | newest stable **v4.1.1** (published 2026-08-20), newest listed `v4.2.0-M2` (prerelease) | it is the release ledger |
| `search.maven.org/solrsearch?core=gav` (what `maven_package` uses) | newest `spring-boot` = **3.5.3** | the Maven Central *search* index is stale: `repo1.maven.org/maven2/org/springframework/boot/spring-boot/maven-metadata.xml` lists 294 versions with `<latest>4.2.0-M2</latest>` and newest stable **4.1.1** |

So "3.5.3" is not a Spring fact, it is a lagging search index: the same query re-fetched minutes apart returns byte-identical 3.5.x results while the repository itself already carries 4.1.1. `maven_package` is left as it is (it reports what its upstream API reports, and its `versions` list is honest about that), but **no Spring tool derives a version from it** — `spring_dependency` takes its Boot version from Initializr, `spring_versions` from GitHub, `spring_reference` from the docs tree it just read. A Spring agent that asks `maven_package("spring-boot")` for "the latest Boot" will get Camel's `org.apache.camel.springboot:spring-boot:4.20.0` anyway, because relevance ranking beats the group you meant.

## Replacing `@enokdev/springdocs-mcp`

The third-party `@enokdev/springdocs-mcp` was configured alongside this server to cover Spring. Its `search_spring_concepts` does not work — measured against the running server, `auto-configuration`, `profiles` and `actuator` all answered `Concept not found in documentation.` — and its other tools duplicate what this package now does against the same upstream pages. The mapping:

| `@enokdev/springdocs-mcp` | This package |
|---|---|
| `search_spring_concepts` (broken) | `spring_search_concepts` — titles **and** in-page headings, with excerpts |
| `get_spring_reference` | `spring_reference` |
| `get_spring_guide` / `get_all_spring_guides` / `search_spring_docs` | `spring_guides` |
| `get_spring_initializr` / `find_spring_dependency` | `spring_initializr` / `spring_dependency` |
| `get_release_notes` / `get_migration_guide` / `compare_spring_versions` | `spring_versions` |
| `java_status`-equivalent health tools | `java_status` / `health_check` |

Recommendation: drop `@enokdev/springdocs-mcp` from the MCP configuration. It is a second scraper of the same four hosts, with its own cache and no politeness layer, and the one capability it had that this package did not (topic search over the reference manual) is now the strongest tool here. Nothing in this package calls it or depends on it.

## Caching & politeness

### Cache

Everything lives in one directory: `~/.cache/java-spring-mcp` by default, overridable with `JAVA_SPRING_MCP_CACHE_DIR`.

| File | Contents | TTL |
|---|---|---|
| `cache.db` | fetched pages (parsed result + raw body + `ETag` / `Last-Modified`), the search index, the harvested Spring page headings, the Spring guides catalogue, Maven metadata, GitHub release lists | JDK docs 7 days, Spring reference pages 7 days, Spring concept index 7 days, Spring guides catalogue 7 days, Initializr metadata 1 day, GitHub releases 1 day, index 7 days, Maven 1 day |
| `robots.db` | the politeness layer's robots.txt cache | 7 days per host |

Delete the files to force a full refresh. A cache problem is never a tool failure: if the directory is unwritable the server simply runs without a cache and says so in `java_status().checks.cache`.

### Politeness

Every outbound request — `fetch_*`, the index build and the `java_status` probes — goes through one small internal module, [`src/java_spring_mcp/politeness.py`](src/java_spring_mcp/politeness.py) (stdlib + `httpx`, no extra dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it ignores wildcards and would report rules such as `Disallow: /*/sp_common/*.htm` as allowed. `docs.oracle.com` publishes over 5,700 rules in a 158 KB file, and among them the older JDK lines — `/en/java/javase/12/` … `/16/` and `/18/` … `/20/`, plus `/javase/6/`, `/7/`, `/9/`, `/10/`, `/1.5.0/` — are disallowed. This server fetches `javase/26`; if an older URL ever reached the layer the answer is `{"ok": false, "error": "blocked by robots.txt for docs.oracle.com — Disallow: /en/java/javase/12/ matches …"}` with **no request made**, not a silent fetch. The javadoc line is a single constant, `fetchers.JDK_API_BASE`, pinned to `javase/26`; the disallow list stops at `/20/`, so `javase/17` and every line from `/21/` up are open to crawlers — repointing that constant at the Java 21 LTS javadoc is the one change that stays robots-clean. Files are cached 7 days per host, including negative results (404 / 403 / 5xx), so a host costs one robots request per week.
- **Per-host throttle, including `Crawl-delay`.** Requests to one host are spaced 0.35–0.9 s apart, or by the site's own `Crawl-delay` when it declares one — `docs.spring.io` declares `crawl-delay: 1` and therefore gets at least one second between requests. A cold run touches three hosts and pays one robots fetch plus one page fetch each, which is exactly where that delay is felt — that is the point (measured: 3 robots fetches + 3 page fetches, 1.56 s spent waiting, `crawl_delay_applied: 1`).
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request is retried once. A response that stalls past the read timeout escalates the delay instead of being retried blindly.
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached result, so refreshing an expired entry costs a `304` instead of a full download. Oracle's javadoc pages send an `ETag` (no `Last-Modified`), so revalidation works there. **`search.maven.org` sends neither `ETag` nor `Last-Modified`** — verified against live response headers — so a conditional GET is not possible for Maven lookups: the fetcher stores no validators for that host and the layer counts the attempt under `conditional_skipped` rather than sending a request that could never yield a `304`. The Spring hosts split the same way and were measured the same way: `docs.spring.io` pages send `Last-Modified` and no `ETag`, while `spring.io`'s `page-data`, `start.spring.io` and `api.github.com` all send `ETag` — either validator is enough, so every Spring fetch stores one.
- **Request budgets.** One tool call may make at most **6 requests in total**, and one budget unit is one request on the wire: the robots fetch, every retry and every redirect hop all pay. That matters here because Oracle answers a wrong class name with a `302` to its landing page rather than a `404`, so nothing in the status line stops the module-guessing loop — the budget does. Measured live: a cold legitimate `java_docs` costs 2–3 units, a cold typo spends all 6 and stops with the error dict above. A standalone index build gets its own `index:docs.oracle.com` budget of 6 and, if it runs out, returns a **partial** index instead of raising.
- **Host allowlist.** Only the hosts this package documents can be contacted — `docs.oracle.com`, `docs.spring.io`, `search.maven.org`, `spring.io`, `start.spring.io` and `api.github.com` — so a malformed identifier or a surprising redirect cannot turn a docs lookup into a request to somebody else's site. What each of them actually publishes (re-measured live, 2026-10-07): `docs.spring.io` declares `User-agent: *` with `crawl-delay: 1` and a second group that disallows `/autorepo/`; `spring.io` allows everything and publishes `Sitemap: https://spring.io/sitemap-index.xml` (the guides catalogue is read from `page-data`, and the sitemap is the fallback source); `start.spring.io` answers `robots.txt` with **404** — no rules, so everything under it is fair game and the negative result is cached for a week; `api.github.com` likewise 404s `robots.txt` and its rate-limit header reported `{"limit": 60, "remaining": 57}`, which is why `spring_versions` fetches one page of 100 releases per project and caches it for a day.
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

.venv/bin/python -m pytest -q              # 257 offline tests; fixtures in tests/fixtures/
.venv/bin/python scripts/politeness_smoke.py /tmp/java-spring-smoke-cache
.venv/bin/python scripts/live_spring_smoke.py /tmp/java-spring-live-cache
.venv/bin/python scripts/e2e_mcp_test.py
```

- `pytest` is fully offline: HTTP is simulated with `httpx.MockTransport` and time/jitter are injected, so the suite is deterministic and runs in seconds. `tests/fixtures/` holds real captured pages (the JDK `List` javadoc, the Spring `using` section, Maven search JSON, the full ~2 MB all-classes index).
- The Spring tools are tested the same way: `tests/test_spring.py` covers the parsers and rankers over fixtures trimmed from live responses (the reference nav, 14 reference pages, a guide page, Initializr's v2.1 metadata, GitHub's release list, Initializr's own generated `pom.xml` / `build.gradle`), and `tests/test_spring_tools.py` drives the six tools through a fixture-backed transport — including the six topic queries (`auto-configuration`, `profiles`, `actuator`, `web`, `data jpa`, `security`), a cache-hit assertion per tool, a request-budget assertion, and the assertion that `spring_dependency` returns `spring-boot-starter-webmvc` for the need `web` because that is what Initializr generates. Note that `spring.py` imports `_http_get` directly, so a test transport has to replace the binding in **both** `fetchers` and `spring`.
- `scripts/politeness_smoke.py <cache-dir>` and `scripts/live_spring_smoke.py <cache-dir>` are **live measurements**, not tests: they drive the real tools against the real docs sites and print what the politeness layer did (requests, robots, throttle, conditional GET, budgets).
- `scripts/e2e_mcp_test.py` spawns the installed `java-spring-docs` console script and speaks newline-delimited JSON-RPC to it (`initialize` → `tools/list` → every tool → a negative case → `health_check`). Exit code 0 means every check passed. Set `JAVA_SPRING_MCP_E2E_CMD` to run a different server command.

## Troubleshooting

**The first `java_docs` call takes half a minute.** That is the cold search-index build: the Oracle all-classes page plus a robots fetch, spaced by the per-host throttle, followed by the page you actually asked for. It happens once every 7 days; later calls hit the cached index. Delete `cache.db` and you pay for it again.

**`request budget exhausted for scope 'tool:java_docs'`.** One tool call hit its cap of 6 requests. Oracle redirects a wrong class name to its landing page instead of returning 404, so a name that is not in the index burns the budget on module guesses before the tool gives up. `java_search` first — it is offline and free — and pass the fully-qualified name. `java_status().politeness.budgets` shows how much each scope spent.

**`"partial": true`, or fewer search results than expected.** An index build ran out of its budget and stopped early, returning a partial index with a `partial_reason`. A partial index is **not cached**, so the next run rebuilds it. `java_search` still works — it just knows fewer classes — and `java_status().checks.search_index.entries` tells you how many it has (a complete JDK 26 index is ~4,700 entries).

**`"stale": true` / `index_stale: true`.** The rebuild failed (offline, DNS failure, 5xx) and the server fell back to the older cached index instead of failing the lookup.

**Nothing is cached and `overall` is `degraded`.** Read `java_status().checks.cache.error` — an unwritable `JAVA_SPRING_MCP_CACHE_DIR` (read-only mount, missing permission) is the usual cause. Tools keep working without a cache; they are just slower and noisier on the network.

**A lookup returns `blocked by robots.txt`.** The site's rules disallow that path for this user agent, and the request was not sent. For Oracle this normally means an older JDK javadoc line: `/en/java/javase/12/`–`/16/` and `/18/`–`/20/` are closed to crawlers. Fetch the page yourself, or accept the consequences of the opt-out switch above.

**`maven_package` returns a group you did not expect.** Without `group_id` the Maven search API ranks by relevance, and a common artifact name can belong to several projects. Pass `group_id` to pin the coordinates.

## Support development

java-spring-mcp is built and maintained by Michal in his spare time. If it saves you time or makes your team's docs easier to work with, a coffee (or more) would mean a lot — every contribution helps keep the project moving. 🙏

**Pay via Revolut:** [revolut.me/michal4zvc](https://revolut.me/michal4zvc)

## License & attribution

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Michal Gaspierik.

The **design** of the politeness layer was inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0): a TTL-cached robots store, the wildcard rule translation, and a per-domain rate limiter with escalating backoff. The implementation is a clean-room rewrite in this project's own synchronous stdlib-plus-`httpx` style — no line was transcribed, translated or mechanically adapted from Crawl4AI, and Crawl4AI is not a dependency of this package. Three defects of the original design are fixed (robots `fetched_at` refresh, negative-result caching, `Crawl-delay` support — the last one matters concretely here, because `docs.spring.io` declares `crawl-delay: 1`). The GPL-3.0 part of Crawl4AI — its vendored `html2text` fork — is deliberately excluded: no code, data or dependency from that tree is used or shipped here. The full statement is in [NOTICE](NOTICE).

Same clean-room architecture as [flutter-mcp](https://github.com/KEEPEE/flutter-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures and an offline test suite.
