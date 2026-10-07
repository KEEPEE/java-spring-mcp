# java-spring-mcp

Real-time **JDK API** (Java SE 26 javadoc), **Spring Boot reference** and **Maven Central** metadata as an MCP (Model Context Protocol) server. Built so AI agents write Java/Spring code against real, current APIs instead of hallucinated or deprecated ones.

- **JDK classes** — scraped live from [docs.oracle.com](https://docs.oracle.com/en/java/javase/26/docs/api/) (all 41 modules: java.base, java.sql, java.desktop, jdk.httpclient, …), ~4700 classes
- **Spring Boot reference** — all 11 sections of the official [Spring Boot reference](https://docs.spring.io/spring-boot/reference/) (using, features, actuator, security, testing, web, data, io, messaging, packaging, index)
- **Maven Central** — artifact search/metadata via the official [search API](https://search.maven.org) + javadoc.io links
- **Local search index** over every JDK class (module + package recorded, so `HttpClient` resolves to the right module automatically), rebuilt at most every 7 days with stale-fallback
- **SQLite TTL cache** so repeated lookups are instant

## Tools

| Tool | What it does |
|---|---|
| `java_docs` | Unified lookup. Identifier forms: `java.util.List` (FQN), `HttpClient` (plain name — resolved via the index to the right module), `spring` / `spring:using` (Spring Boot reference sections). Optional `topic` filter (`methods`, `fields`, `constructors`) and `max_tokens` truncation. |
| `java_search` | Fuzzy search over the JDK class index + Spring sections. Returns ranked results with absolute javadoc URLs — call `java_docs` with the chosen name. |
| `maven_package` | Maven Central artifact metadata: latest version, version list (newest first), artifact URL, javadoc.io link. Optional `group_id` and pinned `version`. |
| `java_status` | Real health check: index size/age/staleness, cache stats, live probes of docs.oracle.com, docs.spring.io, search.maven.org, plus the politeness layer's counters under `politeness`. |
| `health_check` | Server liveness + version. |

All tools return plain dicts; failures come back as `{"error": ..., "suggestion": ...}` — the server never crashes on a bad lookup.

## Requirements

- Python 3.10+ (tested on 3.12)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`) — this package does not support the mcp 2.x rename of FastMCP.
- No `curl`, no browser spoof: every request is one `httpx` call through the politeness layer (see [Politeness](#politeness)).

## Run it

### Option A — `uvx` straight from this repo (no manual install)

```bash
# TOKEN = your GitLab personal access token for github.com/KEEPEE
uvx --from "git+https://github.com/KEEPEE/java-spring-mcp.git" java-spring-docs
```

Add `--refresh` to force re-pulling the latest commit after an update.

### Option B — local venv (fastest startup, no token in config)

```bash
git clone https://github.com/KEEPEE/java-spring-mcp.git
cd java-spring-mcp
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e .
.venv/bin/java-spring-docs        # starts the stdio MCP server
```

## MCP client configuration

### DSH (this machine) — in `~/.dsh-home/profiles/web/cordis.patch.yml`

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

### Claude Desktop / any generic MCP client

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

## Cache location

`~/.cache/java-spring-mcp/cache.db` (override with `JAVA_SPRING_MCP_CACHE_DIR`). JDK/Spring docs cached 7 days, Maven metadata 1 day. The same directory holds `robots.db`, the 7-day robots.txt cache — one env var moves both. Delete the files to force a full refresh.

## Politeness

Every outbound request — `fetch_*`, the search-index build and the `java_status` probes — goes through one small internal layer, `src/java_spring_mcp/politeness.py` (stdlib + `httpx`, no new dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it ignores wildcards, so it misjudges real rules. `docs.oracle.com` disallows the older JDK lines (`/en/java/javase/12/`…`/20/`, `/javase/7|9|10/`) — this server fetches `javase/26`, and if an older URL ever reaches it the answer is `{"ok": false, "error": "… blocked by robots.txt …", "suggestion": …}` with **no request made**, not a silent fetch. Files are cached 7 days per host in `robots.db`, including negative results (404/403/5xx), so a host costs one robots request per week (Oracle's is 158 KB).
- **Per-host throttle, including `Crawl-delay`.** Sequential requests to one host are spaced (0.35–0.9 s by default, or the site's own `Crawl-delay` when it declares one — `docs.spring.io` asks for 1 s and gets ≥1 s). A cold index build is ~33 requests, so this is where the delay is felt — that is the point.
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request retried once. A response that stalls past 10 s escalates the delay instead of being retried blindly.
- **Bounded time, one transport.** 5 s connect / 10 s read / 15 s total, applied per request. There used to be a `curl` subprocess fallback here with 90–105 s timeouts; it is gone — it doubled the worst case to ~2 minutes, could not share robots/throttle/budget state, and the measurement it was written for turned out not to reproduce (see `NOTICE` and the audit note below).
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached result, so refreshing an expired entry costs a `304` instead of re-downloading the page. Hosts that send no validators (`search.maven.org`) are detected and skipped — no conditional request is ever sent there.
- **Request budgets.** One tool call may make at most 6 requests in total — and since the A8 port a budget unit *is* one request on the wire: the robots.txt fetch, every `429`/transport retry and every redirect hop all pay. The index page and the module guesses in `java_docs` share one budget. That matters because Oracle answers a wrong class name with a `302` to its landing page (HTTP 200 on older runs), so nothing in the status line stops the guessing loop — the budget does. Measured live: a cold legit call costs 2–3 units, a cold typo spends all 6 and stops; the same call put **8** requests on the wire when the cap was still 4 *layer calls*. An index build that hits its cap stops early and returns a **partial** index (`"partial": true`) instead of raising, and it is not cached.
- **Host allowlist.** Only `docs.oracle.com`, `docs.spring.io` and `search.maven.org` can be contacted, so a malformed identifier or an unexpected redirect cannot turn a docs lookup into a request somewhere else.

The layer never raises and never changes a tool's return shape; `java_status` reports its counters under the top-level `politeness` key (robots cache rows, throttle waits and per-host delays, blocks, retries, stalls, budgets) — diagnostics only, never part of `overall`.

**Opt-out** (at your own risk — you become responsible for whatever the site's rules say):

```bash
export JAVA_SPRING_MCP_POLITENESS_DISABLED=1
```

That turns off robots, throttle, retry, conditional GET and the allowlist in one switch. There is no partial opt-out.

**Attribution:** the layer's design is inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0) — the robots store, the wildcard translation and the per-domain rate limiter. It is a clean-room rewrite, not a copy, and Crawl4AI is not a dependency here. See [`NOTICE`](NOTICE).

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # offline unit tests (fixtures in tests/fixtures/)
.venv/bin/python scripts/e2e_mcp_test.py      # live end-to-end: spawns the server, exercises every tool
```

## Design notes

Same clean-room architecture as [flutter-mcp](https://github.com/KEEPEE/flutter-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures, and an offline test suite backed by real page fixtures (JDK List javadoc, Spring "using" section, Maven search JSON, the full 2 MB all-classes index).
