"""Wiring tests: do this repo's HTTP paths really go through the politeness layer?

:mod:`tests.test_politeness` proves the layer itself is correct. These tests
prove the *integration* — that ``fetch_*``, the index build, the module-guessing
loop and ``java_status`` cannot bypass it. Everything is offline: the only HTTP
client in play is ``httpx.MockTransport``; time and sleep are injected.

They also pin the five repo-specific decisions of the port:

1. the ``curl`` fallback is gone and the timeouts are bounded (§ 2),
2. ``search.maven.org`` gets no conditional GET and a stall costs one request
   and one delay escalation, never a 105 s hang (§ 3),
3. one tool call spends at most ``FETCH_BUDGET_LIMIT`` requests, so a wrong
   class name is 4 requests and not 6 (§ 4),
4. ``docs.oracle.com`` robots.txt allows ``javase/26`` and blocks the older
   lines — with a readable error, not a silent fetch (§ 1),
5. ``docs.spring.io``'s ``crawl-delay: 1`` is actually waited out (§ 5).

Seams used (all module-level on purpose):

- ``httpx.Client``              → same client, MockTransport swapped in
- ``fetchers.set_politeness``   → a layer with an injected clock / RNG
- ``fetchers.JDK_API_BASE``     → an older JDK line, to test the robots gate
- ``server.load_index``         → keep the index build out of a fetch test
"""

from __future__ import annotations

import ast
import inspect
import random
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

import java_spring_mcp.fetchers as fetchers_mod
import java_spring_mcp.search as search_mod
import java_spring_mcp.server as server_mod
from java_spring_mcp.cache import DocCache
from java_spring_mcp.fetchers import (
    ALLOWED_HOSTS,
    FETCH_BUDGET_LIMIT,
    fetch_jdk_class,
    fetch_maven_artifact,
    fetch_spring_boot_section,
)
from java_spring_mcp.politeness import Politeness
from java_spring_mcp.server import java_status

FIXTURES = Path(__file__).parent / "fixtures"

JDK_LIST_URL = "https://docs.oracle.com/en/java/javase/26/docs/api/java.base/java/util/List.html"
JDK_LIST_PATH = "/en/java/javase/26/docs/api/java.base/java/util/List.html"
JDK_14_URL = "https://docs.oracle.com/en/java/javase/14/docs/api/java.base/java/util/List.html"
ALLCLASSES_PATH = "/en/java/javase/26/docs/api/allclasses-index.html"
SPRING_INDEX_PATH = "/spring-boot/reference/index.html"
SPRING_USING_PATH = "/spring-boot/reference/using/index.html"
MAVEN_PATH = "/solrsearch/select"

# Trimmed excerpt of the real https://docs.oracle.com/robots.txt (the live file
# is 158 KB / 5 745 rules; the audit keeps a copy at
# mcp-politeness-audit/robots/docs.oracle.com.txt).  Everything that matters for
# our paths is here: the ``*`` group, and the disallowed older JDK lines.
ORACLE_ROBOTS = b"""# robots.txt generated 2026-07-24 07:29:56 (trimmed excerpt)
User-agent: Googlebot
Allow: /
User-agent: *
Disallow: /search/
Disallow: /apps/search-client/
Disallow: /pdf/
Allow: /pdf/E*
Disallow: /en/java/javase/12/
Disallow: /en/java/javase/13/
Disallow: /en/java/javase/14/
Disallow: /en/java/javase/15/
Disallow: /en/java/javase/16/
Disallow: /en/java/javase/18/
Disallow: /en/java/javase/19/
Disallow: /en/java/javase/20/
Disallow: /javase/7/
Disallow: /javase/9/
Disallow: /javase/10/
"""

# Verbatim from https://docs.spring.io/robots.txt — two groups for the same
# token (they must be merged) and the crawl-delay we used to ignore.
SPRING_ROBOTS = b"""User-agent: *
crawl-delay: 1

User-agent: *
Disallow: /autorepo/
"""

MAVEN_ROBOTS = b"sitemap: https://search.maven.org/sitemap-index.xml\n"
WELCOME_ROBOTS = b"# nothing here but a sitemap\n"

# Real pages, downloaded once (see tests/test_fetchers.py).
LIST_HTML = (FIXTURES / "jdk_list.html").read_bytes()
SPRING_HTML = (FIXTURES / "spring_using.html").read_bytes()
MAVEN_JSON = (FIXTURES / "maven_springboot.json").read_bytes()

ALLCLASSES_HTML = b"""<html><body><main id="all-classes-table.tabpanel">
<div class="col-first all-classes-table-tab2"><a href="java.base/java/util/List.html"
   title="class in java.util">List</a></div>
</main></body></html>"""

#: What Oracle answers for a class that does not exist: HTTP 200 and a page that
#: is not the requested one (the audit measured the same 33 756 B every time).
ORACLE_SOFT_200_HTML = b"<html><body><main>Available documentation sets</main></body></html>"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class FakeClock:
    """Monotonic clock + a sleep that advances it (no real waiting in tests)."""

    def __init__(self, start: float = 1_000.0, wall: float = 1_700_000_000.0) -> None:
        self.t = start
        self.wall = wall
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def wall_now(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Recorder:
    """MockTransport handler: canned routes + a record of every request."""

    def __init__(self, routes: dict[str, object], *, validators: bool = True) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        #: Whether non-robots responses carry ETag / Last-Modified.  False
        #: models search.maven.org, which sends neither (A2 §3).
        self.validators = validators

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        action = self.routes.get(path, self.routes.get("*"))
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request, self)
        if isinstance(action, int):
            return httpx.Response(action, content=b"nope")
        headers = {}
        if self.validators and path != "/robots.txt":
            headers = {
                "etag": '"v1"',
                "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            }
        return httpx.Response(200, content=action or b"", headers=headers)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def hits(self, path: str) -> int:
        return self.paths().count(path)

    def for_path(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def page_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path != "/robots.txt"]


_REAL_HTTPX_CLIENT = httpx.Client


def use_transport(monkeypatch, recorder: Recorder) -> Recorder:
    """Swap only the *transport* of every ``httpx.Client`` built in the repo.

    Patching the class rather than ``fetchers._client`` keeps the production
    client factory in play, so the real headers (our UA), timeout and
    redirect policy are what reach the wire — and the status probes, which
    build their own client, are covered by the same seam.
    """

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder)
        return _REAL_HTTPX_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return recorder


def fast_layer(clock: FakeClock | None = None, **kw) -> Politeness:
    """Install a politeness layer with no throttling sleeps and return it."""
    kw.setdefault("base_delay", (0.0, 0.0))
    kw.setdefault("rng", random.Random(1234))
    if clock is not None:
        kw["clock"] = clock.now
        kw["sleep"] = clock.sleep
        kw["wall_clock"] = clock.wall_now
    layer = Politeness(fetchers_mod.USER_AGENT, cache_path=None, **kw)
    fetchers_mod.set_politeness(layer)
    return layer


def canned_index(*entries: dict) -> dict:
    return {"built_at": "2026-01-01T00:00:00+00:00", "entries": list(entries)}


LIST_ENTRY = {
    "name": "List",
    "package": "java.util",
    "module": "java.base",
    "kind": "interface",
    "url": JDK_LIST_URL,
}


# ---------------------------------------------------------------------------
# 1. fetch_* really goes through the layer, and robots.txt decides
# ---------------------------------------------------------------------------
def test_fetch_jdk_class_goes_through_the_layer(monkeypatch):
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: LIST_HTML}),
    )

    out = fetch_jdk_class("java.util.List")

    assert out["ok"] is True
    assert "An ordered collection" in out["markdown"]
    paths = rec.paths()
    # robots.txt was consulted *before* the page — that is the whole point.
    assert paths.index("/robots.txt") < paths.index(JDK_LIST_PATH)
    assert rec.hits(JDK_LIST_PATH) == 1


def test_polite_user_agent_is_the_one_on_the_wire(monkeypatch):
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: LIST_HTML}),
    )
    fetch_jdk_class("java.util.List")

    for request in rec.requests:
        assert request.headers["user-agent"] == fetchers_mod.USER_AGENT
    assert "Mozilla" not in fetchers_mod.USER_AGENT
    assert "java-spring-mcp/" in fetchers_mod.USER_AGENT


def test_robots_txt_is_fetched_once_per_host_across_calls(monkeypatch):
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": ORACLE_ROBOTS, "*": LIST_HTML}))

    assert fetch_jdk_class("java.util.List")["ok"] is True
    assert fetch_jdk_class("java.util.Timer")["ok"] is True

    assert rec.hits("/robots.txt") == 1  # 7-day TTL, negative results cached too
    assert rec.hits(JDK_LIST_PATH) == 1


def test_robots_cache_is_a_file_shared_across_layer_instances(monkeypatch, tmp_path):
    db = tmp_path / "robots.db"
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: LIST_HTML}),
    )

    first = Politeness(fetchers_mod.USER_AGENT, cache_path=str(db), base_delay=(0.0, 0.0))
    fetchers_mod.set_politeness(first)
    assert fetch_jdk_class("java.util.List")["ok"] is True

    second = Politeness(fetchers_mod.USER_AGENT, cache_path=str(db), base_delay=(0.0, 0.0))
    fetchers_mod.set_politeness(second)
    assert fetch_jdk_class("java.util.List")["ok"] is True
    second.close()

    assert rec.hits("/robots.txt") == 1  # the second process-wide layer reused it
    assert db.exists()


def test_transport_error_is_retried_once_then_reported(monkeypatch):
    use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: httpx.ConnectError("boom")}),
    )
    layer = fast_layer()

    out = fetch_jdk_class("java.util.List")

    assert out["ok"] is False
    assert "boom" in out["error"]
    # One retry, but the counter counts failed attempts (the reference suite
    # pins this): attempt + retry = 2.
    assert layer.stats()["retries_transport"] == 2
    # Two sends for the page and nothing after that — no third round through
    # a subprocess, which is what the old curl fallback added.
    assert layer.stats()["requests"] == 2


def test_429_with_retry_after_is_honoured_and_retried_once(monkeypatch):
    clock = FakeClock()
    layer = fast_layer(clock)

    def rate_limited(request, rec: Recorder) -> httpx.Response:
        if len(rec.requests) == 2:  # robots was request #1
            return httpx.Response(429, headers={"retry-after": "3"})
        return httpx.Response(200, content=LIST_HTML, headers={"etag": '"v1"'})

    rec = use_transport(monkeypatch, Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: rate_limited}))

    out = fetch_jdk_class("java.util.List")

    assert out["ok"] is True
    assert rec.hits(JDK_LIST_PATH) == 2  # exactly one retry, never three
    assert layer.stats()["retries_429"] == 1
    assert layer.stats()["retry_after_honoured"] == 1
    assert 3.0 in clock.slept  # the server's number, not our guess


def test_opt_out_env_var_bypasses_robots_and_throttle(monkeypatch):
    monkeypatch.setenv("JAVA_SPRING_MCP_POLITENESS_DISABLED", "1")
    layer = Politeness(
        fetchers_mod.USER_AGENT, cache_path=None, base_delay=(0.5, 0.9), rng=random.Random(7)
    )
    fetchers_mod.set_politeness(layer)
    rec = use_transport(monkeypatch, Recorder({JDK_LIST_PATH: LIST_HTML}))

    out = fetch_jdk_class("java.util.List")

    assert layer.disabled is True
    assert out["ok"] is True
    assert rec.hits("/robots.txt") == 0
    assert layer.stats()["throttle_sleep_s"] == 0.0


# ---------------------------------------------------------------------------
# 2. docs.oracle.com robots: javase/26 allowed, the older lines blocked
# ---------------------------------------------------------------------------
def test_current_jdk_line_is_allowed_and_an_older_one_is_blocked(monkeypatch):
    """Both branches of spec item 4 in one test.

    ``docs.oracle.com/robots.txt`` disallows ``/en/java/javase/12/``…``/20/``
    (plus ``/javase/7/``, ``/9/``, ``/10/``).  The repo fetches ``javase/26``,
    which is fine — but a fallback or a hand-written older URL must be refused
    *before* the request, with a message that says why.
    """
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, "*": LIST_HTML}),
    )
    layer = fast_layer()
    client = httpx.Client(transport=httpx.MockTransport(rec))

    assert layer.can_fetch(JDK_LIST_URL, client=client) is True
    assert layer.can_fetch(JDK_14_URL, client=client) is False

    # …and the same through the public fetcher.
    assert fetch_jdk_class("java.util.List")["ok"] is True

    monkeypatch.setattr(
        fetchers_mod, "JDK_API_BASE", "https://docs.oracle.com/en/java/javase/14/docs/api"
    )
    blocked = fetch_jdk_class("java.util.List")

    assert blocked["ok"] is False
    assert "blocked by robots.txt for docs.oracle.com" in blocked["error"]
    assert "Disallow: /en/java/javase/14/" in blocked["error"]
    assert "robots.txt" in blocked["suggestion"]
    assert layer.stats()["blocked_by_robots"] == 1
    # The decisive part: not one byte was fetched from the disallowed path.
    assert not [p for p in rec.paths() if "/javase/14/" in p]


def test_blocked_url_consumes_no_request_and_no_budget(monkeypatch):
    blocked_path = "/autorepo/docs/spring-boot/index.html"  # Disallow: /autorepo/
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": SPRING_ROBOTS, blocked_path: b"x"}),
    )
    layer = fast_layer()

    out = fetchers_mod._http_get(f"https://docs.spring.io{blocked_path}")

    assert out.error and "blocked by robots.txt" in out.error
    assert out.blocked_by_robots is True
    assert rec.hits(blocked_path) == 0
    assert layer.stats()["budget_denied"] == 0  # robots answers before the budget


# ---------------------------------------------------------------------------
# 3. the curl fallback is gone; timeouts are bounded; maven has no validators
# ---------------------------------------------------------------------------
def _imported_top_level_names(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_subprocess_transport_is_left_in_the_http_paths():
    """Spec item 1: the curl fallback is removed, not merely capped.

    An AST check rather than a grep: comments about the old fallback are fine,
    a live ``subprocess`` import in a module that makes requests is not.
    """
    for module in (fetchers_mod, server_mod):
        assert "subprocess" not in _imported_top_level_names(module), module.__name__
        assert not hasattr(module, "_curl_get")
    assert not hasattr(fetchers_mod, "tempfile")


def test_request_timeouts_are_bounded():
    """Spec item 1/2: 20 s httpx + 90 s curl + 105 s subprocess is now 5/10/15."""
    assert fetchers_mod.REQUEST_TIMEOUT == 15.0
    assert fetchers_mod.TIMEOUTS == (5.0, 10.0, 15.0)

    fetchers_mod.set_politeness(None)  # build the production singleton
    try:
        layer = fetchers_mod.get_politeness()
        assert layer.timeout == httpx.Timeout(15.0, connect=5.0, read=10.0)
        assert layer.allowed_hosts == ALLOWED_HOSTS
    finally:
        fetchers_mod.set_politeness(None)


def test_maven_lookup_sends_no_conditional_get_and_stores_no_body(monkeypatch):
    """Spec item 2: search.maven.org sends no ETag / Last-Modified.

    The layer must skip conditional GET there by itself — not be told to.
    """
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": MAVEN_ROBOTS, MAVEN_PATH: MAVEN_JSON}, validators=False),
    )
    layer = fast_layer()

    assert fetch_maven_artifact(None, "spring-boot")["ok"] is True
    assert fetch_maven_artifact(None, "spring-boot")["ok"] is True

    assert rec.hits(MAVEN_PATH) == 2
    for request in rec.for_path(MAVEN_PATH):
        assert "if-none-match" not in request.headers
        assert "if-modified-since" not in request.headers
    stats = layer.stats()
    assert stats["conditional"] == 0
    assert stats["revalidated_304"] == 0

    key = (
        "https://search.maven.org/solrsearch/select?"
        + urlencode({"q": 'a:"spring-boot"', "core": "gav", "rows": 20, "wt": "json"})
    )
    entry = DocCache().get_entry(key)
    assert entry is None or not entry["body"]


def test_stalled_maven_edge_escalates_delay_and_is_not_retried(monkeypatch):
    """Spec item 2: a >10 s answer is a signal, and the tool must not hang.

    A3 §5.5: the timeouts are applied per request through
    ``client.build_request(timeout=…)`` — that is what keeps a stalled edge at
    15 s instead of the old 105 s, and the stall counter is what makes the
    next call wait before reconnecting.
    """
    clock = FakeClock()
    # A non-zero base delay only so the escalation has something to multiply;
    # the clock is fake, so nothing here actually waits.
    layer = fast_layer(clock, base_delay=(0.5, 0.5))

    def stalled(request, rec: Recorder) -> httpx.Response:
        clock.advance(30.0)  # the edge took half a minute to answer
        return httpx.Response(200, content=MAVEN_JSON)

    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": MAVEN_ROBOTS, MAVEN_PATH: stalled}, validators=False),
    )

    out = fetch_maven_artifact(None, "guava")

    assert out["ok"] is True
    assert rec.hits(MAVEN_PATH) == 1  # a stall is not retried — that is amplification
    assert layer.stats()["stalls"] == 1
    assert layer.stats()["hosts"]["search.maven.org"] > 0.0
    # The next request to that host will wait before reconnecting; this call
    # did not sit in a second transport the way the 90 s curl fallback did.
    assert layer.stats()["retries_transport"] == 0
    assert layer.stats()["retries_429"] == 0


def test_docs_oracle_page_is_revalidated_with_a_304(monkeypatch):
    """Conditional GET works where the host *does* send validators (A2 §3)."""
    def page(request, rec: Recorder) -> httpx.Response:
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(
            200,
            content=LIST_HTML,
            headers={"etag": '"v1"', "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT"},
        )

    rec = use_transport(
        monkeypatch, Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: page})
    )
    layer = fast_layer()

    first = fetch_jdk_class("java.util.List")
    assert first["ok"] is True
    entry = DocCache().get_entry(JDK_LIST_URL)
    assert entry and entry["etag"] == '"v1"' and entry["body"]

    second = fetch_jdk_class("java.util.List")
    assert second["ok"] is True
    assert second["markdown"] == first["markdown"]
    assert rec.hits(JDK_LIST_PATH) == 2
    assert rec.for_path(JDK_LIST_PATH)[1].headers["if-none-match"] == '"v1"'
    assert layer.stats()["conditional"] == 1
    assert layer.stats()["revalidated_304"] == 1


# ---------------------------------------------------------------------------
# 4. one tool call, one request budget (spec item 3)
# ---------------------------------------------------------------------------
def test_java_docs_error_path_pays_one_request_per_budget_unit(monkeypatch):
    """Oracle answers a wrong class name with HTTP 200, so only a budget stops it.

    A8 F3 changed what a budget *unit* is: one unit = **one request on the
    wire**, robots.txt included.  The pre-A8 version of this test asserted
    ``pages == FETCH_BUDGET_LIMIT``, which quietly assumed the robots fetch was
    free — under that assumption a limit of 4 was really 5 requests, and 8 on
    Oracle's ``302`` answer (A5 §5).  Now the cap is the wire bill itself.
    """
    layer = fast_layer()
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, "*": ORACLE_SOFT_200_HTML}),
    )
    monkeypatch.setattr(server_mod, "load_index", lambda **kw: canned_index())

    out = server_mod.java_docs("com.example.NopeXYZ")

    assert "error" in out
    assert len(rec.requests) == FETCH_BUDGET_LIMIT          # robots + pages = the cap
    assert rec.hits("/robots.txt") == 1                     # one unit went to robots.txt
    assert len(rec.page_requests()) == FETCH_BUDGET_LIMIT - 1
    assert layer.stats()["budget_denied"] >= 1
    assert "budget" in out["suggestion"]
    # Six modules were configured; only the budgeted ones were ever attempted.
    assert len(server_mod._FALLBACK_MODULES) + 1 > FETCH_BUDGET_LIMIT - 1


def test_module_guess_loop_stops_at_the_denial(monkeypatch):
    """The server must not keep calling a fetcher that can only fail."""
    seen: list[str] = []

    def fake_jdk(fully_qualified, module="java.base"):
        seen.append(module)
        return {"ok": False, "error": "request budget exhausted for scope 'tool:java_docs'",
                "budget_exhausted": True}

    monkeypatch.setattr(server_mod, "fetch_jdk_class", fake_jdk)
    monkeypatch.setattr(server_mod, "load_index", lambda **kw: canned_index())

    out = server_mod.java_docs("com.example.NopeXYZ")

    assert seen == ["java.base"]  # one attempt, then the loop breaks
    assert "budget" in out["suggestion"]


def test_cold_index_and_module_guesses_share_one_budget(monkeypatch):
    """The index build is not a back door around the per-call cap.

    F3 accounting again: robots.txt, the index page and every module guess all
    pay from the same ``tool:java_docs`` scope, and the total is the cap.
    """
    layer = fast_layer()
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": ORACLE_ROBOTS,
                ALLCLASSES_PATH: ALLCLASSES_HTML,
                "*": ORACLE_SOFT_200_HTML,
            }
        ),
    )

    server_mod.java_docs("com.example.NopeXYZ")

    assert len(rec.requests) == FETCH_BUDGET_LIMIT       # robots + pages
    assert rec.hits("/robots.txt") == 1
    pages = rec.page_requests()
    assert len(pages) == FETCH_BUDGET_LIMIT - 1
    assert any(r.url.path == ALLCLASSES_PATH for r in pages)  # the index took a slot
    assert layer.stats()["budget_denied"] >= 1


def test_index_budget_exhaustion_yields_a_partial_index_not_an_exception(monkeypatch):
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, ALLCLASSES_PATH: ALLCLASSES_HTML}),
    )

    with fetchers_mod.tool_budget("tiny", limit=0):
        index = search_mod.build_index()

    assert index["partial"] is True
    assert "budget" in index["partial_reason"]
    assert rec.hits(ALLCLASSES_PATH) == 0
    # The offline Spring entries survive, so ``spring:using`` still resolves.
    assert any(e["module"] == "spring-boot" for e in index["entries"])


def test_partial_index_is_never_cached(monkeypatch):
    use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, ALLCLASSES_PATH: ALLCLASSES_HTML}),
    )

    with fetchers_mod.tool_budget("tiny", limit=0):
        out = search_mod.load_index()

    assert out["partial"] is True
    assert DocCache().get(search_mod.INDEX_KEY) is None
    # …and a full build afterwards is cached normally.
    full = search_mod.load_index()
    assert not full.get("partial")
    assert DocCache().get(search_mod.INDEX_KEY) is not None


def test_tool_budget_is_reset_per_call(monkeypatch):
    """Stable scope name + reset on entry: budgets cannot grow unbounded."""
    use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: LIST_HTML}),
    )
    fast_layer()
    for _ in range(3):
        with fetchers_mod.tool_budget("java_docs"):
            fetchers_mod._http_get(JDK_LIST_URL, revalidate=False)
    stats = fetchers_mod.get_politeness().stats()
    # [used, limit] — used is 1, not 3: the scope was reset on entry, so
    # ``stats()["budgets"]`` stays small and never starves later calls.
    assert stats["budgets"] == {"tool:java_docs": [1, FETCH_BUDGET_LIMIT]}


# ---------------------------------------------------------------------------
# 5. docs.spring.io crawl-delay: 1 is actually waited out
# ---------------------------------------------------------------------------
def test_spring_crawl_delay_of_one_second_is_respected(monkeypatch):
    clock = FakeClock()
    layer = fast_layer(clock)
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": SPRING_ROBOTS,
                SPRING_INDEX_PATH: SPRING_HTML,
                SPRING_USING_PATH: SPRING_HTML,
            }
        ),
    )

    assert fetch_spring_boot_section("index")["ok"] is True
    assert fetch_spring_boot_section("using")["ok"] is True

    assert rec.hits(SPRING_INDEX_PATH) == 1
    assert rec.hits(SPRING_USING_PATH) == 1
    waits = [s for s in clock.slept if s > 0]
    assert waits, "crawl-delay: 1 must produce a real wait"
    assert min(waits) >= 1.0
    stats = layer.stats()
    assert stats["throttle_waits"] >= 1  # the second page had to wait for the delay
    assert stats["throttle_sleep_s"] >= 1.0


def test_spring_crawl_delay_gates_the_first_page_after_a_cold_robots_fetch(monkeypatch):
    """A13 B2, at the repo level, on the host where A12 measured the defect.

    A12: after a cold robots fetch the gap to the first ``docs.spring.io`` page
    was 365 / 711 / 814 / 827 / 857 ms on 5/5 runs, although the file declares
    ``crawl-delay: 1``.  The delay was applied one request too late.  It is now
    anchored on the robots request itself, so the very first page request obeys
    it — and the robots fetch is still throttled (A8 F1 untouched).
    """
    clock = FakeClock()
    layer = fast_layer(clock)
    stamps: list[tuple[str, float]] = []

    def record(body: bytes):
        def handler(request: httpx.Request, recorder: Recorder) -> httpx.Response:
            stamps.append((request.url.path, clock.now()))
            return httpx.Response(200, content=body)

        return handler

    use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": record(SPRING_ROBOTS),
                SPRING_INDEX_PATH: record(SPRING_HTML),
            }
        ),
    )

    assert fetch_spring_boot_section("index")["ok"] is True
    assert [p for p, _ in stamps] == ["/robots.txt", SPRING_INDEX_PATH], stamps
    gap = stamps[1][1] - stamps[0][1]
    assert gap >= 1.0, f"first page request was {gap * 1000:.0f} ms after the robots fetch"
    assert layer.stats()["crawl_delay_applied"] == 1


def test_other_hosts_are_not_delayed_by_the_spring_crawl_delay(monkeypatch):
    """crawl-delay is per host — docs.oracle.com does not inherit it."""
    clock = FakeClock()
    layer = fast_layer(clock)
    use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: LIST_HTML, "*": LIST_HTML}),
    )

    assert fetch_jdk_class("java.util.List")["ok"] is True
    assert fetch_jdk_class("java.util.Timer")["ok"] is True

    assert clock.slept == []  # not one wait on a host that asked for none
    stats = layer.stats()
    assert stats["throttle_waits"] == 0 and stats["throttle_sleep_s"] == 0.0


# ---------------------------------------------------------------------------
# 6. java_status exposes the counters without changing its verdict
# ---------------------------------------------------------------------------
def test_java_status_reports_politeness_outside_checks(monkeypatch):
    fast_layer()
    # The probes build their own httpx.Client; use_transport covers that too.
    use_transport(monkeypatch, Recorder({"/robots.txt": WELCOME_ROBOTS, "*": b"ok"}, validators=False))
    monkeypatch.setattr(server_mod, "load_index", lambda **kw: canned_index(LIST_ENTRY))

    out = java_status()

    # Public shape unchanged…
    assert out["server"] == "java-spring-mcp"
    assert set(out["checks"]) == {
        "search_index",
        "cache",
        "docs_oracle_com",
        "docs_spring_io",
        "search_maven_org",
    }
    assert out["overall"] == "ok"
    # …and the counters ride along as an extra top-level block.
    pol = out["politeness"]
    assert pol["status"] == "ok" and pol["disabled"] is False
    assert pol["requests"] >= 3  # the three probes went through the layer
    assert pol["robots_fetches"] == 3  # one per probed host
    assert pol["robots_rows"] == 0  # in-memory layer: no robots.db file yet
    for key in (
        "robots_cache_hits", "blocked_by_robots", "throttle_waits", "throttle_sleep_s",
        "host_delays", "budgets", "budget_denied", "conditional", "revalidated_304",
        "retries_429", "retries_transport", "stalls", "errors",
    ):
        assert key in pol, key
    assert "politeness" not in out["checks"]


def test_politeness_diagnostics_cannot_degrade_overall(monkeypatch):
    monkeypatch.setattr(server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200})
    monkeypatch.setattr(server_mod, "load_index", lambda **kw: canned_index(LIST_ENTRY))
    monkeypatch.setattr(server_mod, "_politeness_report", lambda: {"status": "error", "error": "boom"})

    out = java_status()

    assert out["overall"] == "ok"
    assert out["politeness"] == {"status": "error", "error": "boom"}


def test_status_probes_are_blocked_by_robots_like_any_other_request(monkeypatch):
    """A health check that ignored robots.txt would be a hole in the layer."""
    fast_layer()
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": ORACLE_ROBOTS, "*": b"ok"}, validators=False))
    monkeypatch.setattr(server_mod, "load_index", lambda **kw: canned_index(LIST_ENTRY))
    # Point the Oracle probe at a disallowed older line.
    monkeypatch.setattr(
        server_mod,
        "_ENDPOINT_PROBES",
        (("docs_oracle_com", JDK_14_URL),),
    )

    out = java_status()

    check = out["checks"]["docs_oracle_com"]
    assert check["status"] == "error"
    assert "blocked by robots.txt" in check["error"]
    assert rec.hits("/en/java/javase/14/docs/api/java.base/java/util/List.html") == 0


def test_server_cache_write_does_not_clobber_fetcher_validators(monkeypatch):
    """``set_value`` vs ``set``: the parsed doc and the raw body share one row."""
    use_transport(
        monkeypatch,
        Recorder({"/robots.txt": ORACLE_ROBOTS, JDK_LIST_PATH: LIST_HTML}),
    )
    monkeypatch.setattr(server_mod, "load_index", lambda **kw: canned_index(LIST_ENTRY))

    out = server_mod.java_docs("java.util.List")
    assert "content" in out and "List" in (out.get("title") or "")

    entry = DocCache().get_entry(JDK_LIST_URL)
    assert entry is not None
    assert entry["body"] and entry["etag"] == '"v1"'  # written by the fetcher
    assert entry["value"] and "markdown" in entry["value"]  # written by the server


# ---------------------------------------------------------------------------
# A13 B3 — an unwritable cache directory must never take a tool down
# ---------------------------------------------------------------------------
def test_maven_package_and_status_survive_an_unwritable_cache_dir(monkeypatch, tmp_path):
    """A12 F-A12-2: the cache is optional end-to-end in this repo too.

    ``DocCache()`` failing is caught, the tool still answers from the wire, and
    ``java_status`` reports the cache as unusable rather than silently saying
    "ok" about a cache that does not exist.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where a directory should be")
    monkeypatch.setenv("JAVA_SPRING_MCP_CACHE_DIR", str(blocker / "cache"))

    fast_layer()
    use_transport(
        monkeypatch,
        Recorder({"/robots.txt": MAVEN_ROBOTS, MAVEN_PATH: MAVEN_JSON}, validators=False),
    )

    out = server_mod.maven_package("spring-boot")
    assert "error" not in out, out
    assert out.get("version"), out

    monkeypatch.setattr(server_mod, "load_index", lambda **kw: canned_index(LIST_ENTRY))
    status = java_status()
    assert status["checks"]["cache"]["status"] == "error", status["checks"]["cache"]
    assert "error" in status["checks"]["cache"]     # the reason is spelled out
    assert status["overall"] == "degraded"          # announced, not hidden
