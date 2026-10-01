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
| `java_status` | Real health check: index size/age/staleness, cache stats, live probes of docs.oracle.com, docs.spring.io, search.maven.org (with curl fallback for edges that stall Python TLS). |
| `health_check` | Server liveness + version. |

All tools return plain dicts; failures come back as `{"error": ..., "suggestion": ...}` — the server never crashes on a bad lookup.

## Requirements

- Python 3.10+ (tested on 3.12)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`) — this package does not support the mcp 2.x rename of FastMCP.
- `curl` on PATH (fallback transport for hosts that throttle Python TLS clients, e.g. search.maven.org)

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

`~/.cache/java-spring-mcp/cache.db` (override with `JAVA_SPRING_MCP_CACHE_DIR`). JDK/Spring docs cached 7 days, Maven metadata 1 day. Delete the file to force a full refresh.

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # offline unit tests (fixtures in tests/fixtures/)
.venv/bin/python scripts/e2e_mcp_test.py      # live end-to-end: spawns the server, exercises every tool
```

## Design notes

Same clean-room architecture as [flutter-mcp](https://github.com/KEEPEE/flutter-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures, and an offline test suite backed by real page fixtures (JDK List javadoc, Spring "using" section, Maven search JSON, the full 2 MB all-classes index).
