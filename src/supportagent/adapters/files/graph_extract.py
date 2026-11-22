"""Structured graph facts (ADR-0015 §3 layers 1 and 2, WP-O3): config, CMDB, live discovery.

Pass 1 finds the **components** each repository builds:

- a Maven ``pom.xml``: ``service:<artifactId>``, or ``job:`` for Spring Batch;
- an Angular ``package.json``: ``ui:<name>``;
- a PySpark script: ``job:<repo>``; a Streamlit app: ``ui:<repo>``;
- a Kafka Connect ``connector.json``: ``job:<connector name>``;
- a Snowflake ``CREATE TASK``: ``job:<task>``.

Pass 2 reads what they connect to: JDBC URLs, gateway routes and client
base URLs, third-party hosts, Helm namespaces, Kafka subscriptions and
bootstrap servers, Snowflake ``dbtable`` and ``sfWarehouse``, SQL ``INTO`` and
``FROM``, Airflow operators, EMR clusters. Then the CMDB (installed CIs,
typed relationships) and live discovery add theirs.

Names are reconciled by general rules, not per system: a CMDB CI whose words
are all in exactly one code component's name is that component (a CMDB
``daily-sales-task`` is the code's ``task_build_daily_sales``); a Kafka
Connect consumer group ``connect-<name>`` is connector ``<name>``; a runtime
named without its cluster (a Helm ``namespace`` alone) is the one known
runtime with that name; a CMDB CI named like a known runtime is that runtime.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

from supportagent.adapters.files.code_python import CodeFinding, python_findings
from supportagent.domain.graph import AliasFact, EdgeFact, Fact, NodeFact, NodeKind
from supportagent.domain.model.plugin import (
    PLACEHOLDER,
    Discovery,
    Extractor,
    ExtractRule,
    NodeTemplate,
)
from supportagent.domain.plugins import path_matches

_POM_ARTIFACT = re.compile(r"</parent>\s*.*?<artifactId>([^<]+)</artifactId>", re.S)
#: An MSK broker host names its cluster: ``b-1.<cluster>[.<id>.c2].kafka.<region>...``.
_MSK = re.compile(r"b-\d+\.([a-z0-9-]+)\.(?:[a-z0-9]+\.)*kafka\.")
_JDBC = re.compile(r"jdbc:(oracle|postgresql|mysql|sqlserver)[^\s\"']*?[/:@]([A-Za-z0-9_]+)\s*$")
_URL_HOST = re.compile(r"^[a-z]+://([^/:]+)")
_SVC_HOST = re.compile(r"^([a-z0-9-]+)\.[a-z0-9-]+\.svc(\.|$)")
_SNOW_TABLE = re.compile(r"\b([A-Z][A-Z0-9_]*)\.([A-Z][A-Z0-9_]*)\b")
_TASK = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?TASK\s+(?:[A-Z0-9_]+\.)?([A-Z0-9_]+)", re.I)
_TASK_REF = re.compile(r"EXECUTE\s+TASK\s+(?:[A-Z0-9_]+\.)?([A-Z0-9_]+)", re.I)
_WAREHOUSE = re.compile(r"WAREHOUSE\s*=\s*([A-Z0-9_]+)", re.I)
_OPTION = re.compile(r'\.option\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)')
_CI_RUNTIME = {"cmdb_ci_kubernetes_namespace": "k8s"}
#: CMDB classes the core maps to nodes; plugins declare the rest (ADR-0016).
_CI_KIND: Mapping[str, NodeKind] = {
    "cmdb_ci_service_auto": "Service",
    "cmdb_ci_appl": "Service",
    "cmdb_ci_db_ora_instance": "Database",
}


@dataclass(slots=True)
class _Collect:
    facts: list[Fact] = field(default_factory=list)
    #: repo -> its component id
    components: dict[str, str] = field(default_factory=dict)
    #: node id -> the kind it was first asserted with
    kinds: dict[str, NodeKind] = field(default_factory=dict)
    #: ADR-0023 §3: (repo, file declaring it, component) -> its build context and dockerfile
    builds: dict[tuple[str, str, str], dict[str, str]] = field(default_factory=dict)
    #: (repo, path prefix) -> the components whose image copies that code
    code_roots: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    #: code files whose facts no component owns
    unowned: set[str] = field(default_factory=set)

    def node(
        self,
        node_id: str,
        kind: NodeKind,
        source: str,
        *,
        layer: int = 1,
        tech: Iterable[str] = (),
        by: str,
    ) -> None:
        self.kinds.setdefault(node_id, kind)
        self.facts.append(NodeFact(node_id, kind, source, layer, by, tuple(tech)))  # type: ignore[arg-type]

    def edge(self, src: str, kind: str, dst: str, source: str, *, layer: int = 1, by: str) -> None:
        self.facts.append(EdgeFact(src, kind, dst, source, layer, by))  # type: ignore[arg-type]

    def alias(self, alias: str, canonical: str, source: str, *, layer: int = 1, by: str) -> None:
        self.facts.append(AliasFact(alias, canonical, source, layer, by))  # type: ignore[arg-type]


def structured_facts(
    files: Mapping[str, str],
    cmdb: Mapping[str, Any],
    live: Mapping[str, Any],
    declared: Sequence[tuple[str, Extractor]] = (),
    discovery: Sequence[tuple[str, str, Discovery]] = (),
) -> list[Fact]:
    """Every layer-1 (config, CMDB) and layer-2 (live) fact about the system.

    ``declared`` (ADR-0016) is each plugin's declarative extractors, as
    ``(plugin id, extractor)``: they run after the built-in ones, a component
    rule only for a repository no built-in claimed.

    ``discovery`` is each plugin's live mapping, as ``(plugin id, capability,
    discovery)``: a live result whose capability no plugin maps adds nothing.
    """
    out = _Collect()
    by_repo: dict[str, dict[str, str]] = {}
    for key, text in files.items():
        repo, _, path = key.partition("/")
        by_repo.setdefault(repo, {})[path] = text
    for repo, repo_files in sorted(by_repo.items()):
        _components(out, repo, repo_files)
    _declared(out, by_repo, declared, own=True)
    for repo, repo_files in sorted(by_repo.items()):
        for path, text in sorted(repo_files.items()):
            for matches, extractor in _RELATIONS:
                if matches(path):
                    extractor(out, repo, path, text)
    _declared(out, by_repo, declared, own=False)
    _code(out, files, declared, set(by_repo))
    _cmdb(out, cmdb, {e.match.removeprefix("cmdb:"): e.node for _, e in declared if e.node})
    _live(out, live, discovery)
    _reconcile(out)
    return out.facts


# --- ADR-0016: plugin-declared rules --------------------------------------------------


def _declared(
    out: _Collect,
    by_repo: Mapping[str, Mapping[str, str]],
    declared: Sequence[tuple[str, Extractor]],
    *,
    own: bool,
) -> None:
    for plugin_id, extractor in declared:
        rules = [r for r in extractor.rules if r.own == own]
        if not rules or extractor.format is None:
            continue
        for repo, repo_files in sorted(by_repo.items()):
            for path, text in sorted(repo_files.items()):
                if not path_matches(f"{repo}/{path}", extractor.match):
                    continue
                leaves = _flatten(extractor.format, text)
                source = f"git:{repo}/{path}"
                for rule in rules:
                    key = re.compile(rule.key)
                    value = re.compile(rule.value)
                    for leaf, text_value in leaves:
                        if (k := key.search(leaf)) and (m := value.search(text_value)):
                            groups = {**k.groupdict(), **m.groupdict()}
                            if rule.code_root and rule.component is not None:
                                _code_root(out, repo, path, rule, groups, by_repo)
                            _apply(out, repo, source, rule, groups, by=f"plugin:{plugin_id}")


def _apply(
    out: _Collect,
    repo: str,
    source: str,
    rule: ExtractRule,
    captured: Mapping[str, str | None],
    *,
    by: str,
) -> None:
    groups = {k: _slug(v) for k, v in captured.items() if v}
    owner = out.components.get(repo)
    if rule.component is not None:
        owner = rule.component.id.format(**groups)
        out.node(owner, rule.component.kind, source, tech=rule.component.tech, by=by)
    if rule.code_root:
        return
    if rule.own and rule.node is not None:
        if owner is None:
            node_id = rule.node.id.format(**groups)
            out.node(node_id, rule.node.kind, source, tech=rule.node.tech, by=by)
            out.components[repo] = node_id
        return
    if owner is None:
        return
    if rule.tech:
        out.node(owner, out.kinds.get(owner, "Service"), source, tech=rule.tech, by=by)
    if rule.node is not None:
        node_id = rule.node.id.format(**groups)
        out.node(node_id, rule.node.kind, source, tech=rule.node.tech, by=by)
        if rule.edge is not None:
            out.edge(owner, rule.edge, node_id, source, by=by)
    elif rule.to is not None and rule.edge is not None:
        out.edge(owner, rule.edge, rule.to.format(**groups), source, by=by)


def _code_root(  # noqa: PLR0917 — one rule match, and where it sits
    out: _Collect,
    repo: str,
    path: str,
    rule: ExtractRule,
    captured: Mapping[str, str | None],
    by_repo: Mapping[str, Mapping[str, str]],
) -> None:
    """Record where a component is built from; :func:`_resolve_code_roots` reads it."""
    del by_repo
    raw = captured.get("name")
    if not raw or rule.component is None or rule.code_root is None:
        return
    component = rule.component.id.format(**{k: _slug(v) for k, v in captured.items() if v})
    out.builds.setdefault((repo, path, component), {})[rule.code_root] = raw.strip()


_COPY = re.compile(r"^\s*(?:COPY|ADD)\s+(.+)$", re.I | re.M)


def _resolve_code_roots(out: _Collect, by_repo: Mapping[str, Mapping[str, str]]) -> None:
    """A component owns the code its image copies (ADR-0023 §3).

    The build context is relative to the declaring file; one outside its
    repository names another repository by its last segment (a Compose file
    beside the checkouts). The Dockerfile (``Dockerfile`` in the context
    unless named) lists what is copied in: each ``COPY``/``ADD`` source, except
    from another stage, is the component's code. Without a readable Dockerfile
    the Dockerfile's directory, or else the context, is.
    """
    for (repo, path, component), build in sorted(out.builds.items()):
        context = build.get("context", ".")
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), context))
        if joined.startswith("..") or posixpath.isabs(context):
            ctx_repo = posixpath.basename(posixpath.normpath(context))
            if ctx_repo not in by_repo:
                continue
            ctx_dir = ""
        else:
            ctx_repo, ctx_dir = repo, ("" if joined == "." else joined)
        dockerfile = _join(ctx_dir, build.get("dockerfile", "Dockerfile"))
        text = by_repo[ctx_repo].get(dockerfile)
        roots = _copied(text, ctx_dir) if text is not None else []
        if not roots:
            fallback = posixpath.dirname(dockerfile) if "dockerfile" in build else ctx_dir
            roots = [fallback]
        for root in roots:
            out.code_roots.setdefault((ctx_repo, root), set()).add(component)


def _copied(dockerfile: str, ctx_dir: str) -> list[str]:
    text = re.sub(r"\\\n", " ", dockerfile)
    roots: list[str] = []
    for match in _COPY.finditer(text):
        args = match.group(1).strip()
        if args.startswith("["):
            try:
                parts = [str(a) for a in json.loads(args)]
            except ValueError:
                continue
        else:
            parts = args.split()
        if any(p.startswith("--from") for p in parts):
            continue
        sources = [p for p in parts if not p.startswith("--")][:-1]
        for source in sources:
            cleaned = source.split("*", 1)[0].rstrip("/")
            roots.append(_join(ctx_dir, cleaned) if cleaned not in ("", ".") else ctx_dir)
    return roots


def _join(base: str, rel: str) -> str:
    joined = posixpath.normpath(posixpath.join(base, rel)) if base else posixpath.normpath(rel)
    return "" if joined == "." else joined


def _code(
    out: _Collect,
    files: Mapping[str, str],
    declared: Sequence[tuple[str, Extractor]],
    repos: set[str],
) -> None:
    """The code layer (ADR-0023 §3): plugin code rules, each fact citing its line."""
    rules = [
        (plugin_id, rule)
        for plugin_id, extractor in declared
        if extractor.format == "python"
        for rule in extractor.code
    ]
    if not rules:
        return
    by_repo: dict[str, dict[str, str]] = {}
    for key, text in files.items():
        repo, _, path = key.partition("/")
        by_repo.setdefault(repo, {})[path] = text
    _resolve_code_roots(out, by_repo)
    report = python_findings({k: v for k, v in files.items() if k.endswith(".py")}, rules)
    for finding in report.findings:
        owners = _code_owners(out, finding.path, repos)
        if not owners:
            out.unowned.add(finding.path)
            continue
        for owner in sorted(owners):
            _code_fact(out, owner, finding)
    _same_entities(out)


#: A pattern covers others only with this much literal text (``*_*`` covers nothing).
_MIN_LITERAL = 4


def _same_entities(out: _Collect) -> None:
    """Two names for one entity, by general rules (ADR-0023 §3):

    - a pattern covered by a broader one is that one (``table:*_*_kline_1m``
      read here is the ``table:*_*_kline_*`` written there);
    - a component named with ``_`` where another uses ``-`` is that one
      (``celery_worker`` in one Compose file, ``celery-worker`` in another).
    """
    nodes = sorted({f.id for f in out.facts if isinstance(f, NodeFact)})
    patterns = [n for n in nodes if "*" in n]
    for node in patterns:
        prefix, _, name = node.partition(":")
        broader = [
            p
            for p in patterns
            if p != node
            and p.startswith(prefix + ":")
            and fnmatch.fnmatchcase(name, p.partition(":")[2])
            and len(p.partition(":")[2].replace("*", "")) >= _MIN_LITERAL
        ]
        if broader:
            widest = min(broader, key=lambda p: len(p.replace("*", "")))
            out.alias(node, widest, "reconcile", by="reconcile")
    known = set(nodes)
    renamed: dict[str, str] = {}
    for node in nodes:
        prefix, _, name = node.partition(":")
        hyphen = f"{prefix}:{name.replace('_', '-')}"
        if prefix in ("service", "job", "ui") and "_" in name and hyphen in known:
            out.alias(node, hyphen, "reconcile", by="reconcile")
            renamed[name] = hyphen.partition(":")[2]
    for node in nodes:  # a component's jobs follow it (``job:<owner>.<name>``)
        prefix, _, name = node.partition(":")
        owner, dot, rest = name.partition(".")
        if prefix == "job" and dot and owner in renamed:
            out.alias(node, f"job:{renamed[owner]}.{rest}", "reconcile", by="reconcile")


def _code_owners(out: _Collect, key: str, repos: set[str]) -> set[str]:
    repo, _, path = key.partition("/")
    best: tuple[int, set[str]] = (-1, set())
    for (root_repo, root_dir), components in out.code_roots.items():
        inside = root_dir in ("", path) or path.startswith(root_dir + "/")
        if root_repo != repo or not inside:
            continue
        if len(root_dir) > best[0]:
            best = (len(root_dir), components)
    if best[0] >= 0:
        return best[1]
    own = out.components.get(repo) if repo in repos else None
    return {own} if own else set()


def _code_fact(out: _Collect, owner: str, finding: CodeFinding) -> None:
    rule = finding.rule
    groups = {k: _code_slug(v) for k, v in finding.groups.items()}
    groups["owner"] = owner.split(":", 1)[1]
    node_id = rule.node.id.format(**groups)
    source = f"git:{finding.path}:{finding.line}"
    by = f"plugin:{finding.plugin}/{rule.id}"
    out.node(node_id, rule.node.kind, source, tech=rule.node.tech, by=by)
    if rule.reverse:
        out.edge(node_id, rule.edge, owner, source, by=by)
    else:
        out.edge(owner, rule.edge, node_id, source, by=by)


def _code_slug(value: str) -> str:
    """Like :func:`_slug`, but a ``*`` (an unresolved part) stays a ``*``."""
    return re.sub(r"[^a-z0-9._/*-]+", "-", value.strip().lower()).strip("-")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._/-]+", "-", value.strip().lower()).strip("-")


def _flatten(fmt: str, text: str) -> list[tuple[str, str]]:
    """A config file's ``(dotted key, string value)`` leaves; an unreadable file has none."""
    if fmt == "lines":
        out = []
        for line in text.splitlines():
            key, _, value = line.strip().partition(" ")
            if key and not key.startswith(("#", "//")) and value:
                out.append((key, value.strip().strip("'\"")))
        return out
    if fmt in ("env", "properties"):
        out = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "!")):
                continue
            key, sep, value = stripped.partition("=")
            if not sep and fmt == "properties":
                key, sep, value = stripped.partition(":")
            if sep:
                out.append(
                    (key.strip().removeprefix("export ").strip(), value.strip().strip("'\""))
                )
        return out
    try:
        data = json.loads(text) if fmt == "json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):
        return []
    return [(k, v) for k, v in _leaves(data) if isinstance(v, str)]


# --- pass 1: components -------------------------------------------------------------


def _components(out: _Collect, repo: str, files: Mapping[str, str]) -> None:
    for path, text in sorted(files.items()):
        source = f"git:{repo}/{path}"
        name = path.rsplit("/", 1)[-1]
        if name == "pom.xml" and (m := _POM_ARTIFACT.search(text)):
            batch = "spring-boot-starter-batch" in text
            kind: NodeKind = "Job" if batch else "Service"
            tech = ["java", "spring-boot"] + (["spring-batch"] if batch else [])
            if "spring-cloud-starter-gateway" in text:
                tech.append("spring-cloud-gateway")
            node_id = f"{'job' if batch else 'service'}:{m.group(1)}"
            out.node(node_id, kind, source, tech=tech, by="pom")
            out.components[repo] = node_id
        elif name == "package.json" and '"@angular/core"' in text:
            node_id = f"ui:{json.loads(text).get('name', repo)}"
            out.node(node_id, "UI", source, tech=["angular"], by="package_json")
            out.components[repo] = node_id
        elif name == "connector.json":
            config = json.loads(text)
            node_id = f"job:{config.get('name', repo)}"
            out.node(node_id, "Job", source, tech=["kafka-connect"], by="kafka_connect")
            out.components[repo] = node_id
        elif name.endswith(".py") and "import streamlit" in text:
            out.node(f"ui:{repo}", "UI", source, tech=["streamlit"], by="streamlit")
            out.components[repo] = f"ui:{repo}"
        elif name.endswith(".py") and "SparkSession" in text:
            tech = ["pyspark"] + (["spark-streaming"] if "readStream" in text else [])
            out.node(f"job:{repo}", "Job", source, tech=tech, by="pyspark")
            out.components[repo] = f"job:{repo}"
        elif name.endswith(".sql"):
            for task in _TASK.findall(text):
                out.node(
                    f"job:{task.lower()}",
                    "Job",
                    source,
                    tech=["snowflake-task"],
                    by="snowflake_sql",
                )


# --- pass 2: relations --------------------------------------------------------------


def _owner(out: _Collect, repo: str) -> str | None:
    return out.components.get(repo)


def _spring_config(out: _Collect, repo: str, path: str, text: str) -> None:
    owner = _owner(out, repo)
    if owner is None:
        return
    source = f"git:{repo}/{path}"
    data = yaml.safe_load(text) or {}
    for key, value in _leaves(data):
        if not isinstance(value, str):
            continue
        if m := _JDBC.search(value):
            db = f"database:{m.group(2).lower()}"
            out.node(db, "Database", source, tech=[m.group(1)], by="jdbc")
            out.edge(owner, "uses", db, source, by="jdbc")
            continue
        if key.endswith("kafka.bootstrap-servers") and (m := _MSK.search(value)):  # WP-O8
            cluster = f"runtime:kafka/{m.group(1)}"
            out.node(cluster, "Runtime", source, tech=["kafka"], by="spring_kafka")
            out.edge(owner, "uses", cluster, source, by="spring_kafka")
            continue
        host = _host(value)
        if host is None or "origin" in key.lower():
            continue
        if svc := _SVC_HOST.match(host):
            out.edge(owner, "calls", f"service:{svc.group(1)}", source, by="service_url")
        elif key.endswith(("base-url", "url", "host")) and not _internal(host):
            org = host.split(".")[-2]
            out.node(f"external:{org}", "External", source, by="third_party_host")
            out.edge(owner, "depends_on", f"external:{org}", source, by="third_party_host")
    for key, value in _leaves(data):
        h = _host(value) if key.endswith("uri") and isinstance(value, str) else None
        if h and (svc := _SVC_HOST.match(h)):
            out.edge(owner, "calls", f"service:{svc.group(1)}", source, by="gateway_route")


def _helm(out: _Collect, repo: str, path: str, text: str) -> None:
    owner = _owner(out, repo)
    data = yaml.safe_load(text) or {}
    namespace = data.get("namespace") if isinstance(data, dict) else None
    if owner is None or not namespace:
        return
    source = f"git:{repo}/{path}"
    cluster = data.get("cluster") or "*"
    runtime = f"runtime:{cluster}/{namespace}"
    if cluster != "*":
        out.node(runtime, "Runtime", source, tech=["kubernetes"], by="helm")
    out.edge(owner, "runs_on", runtime, source, by="helm")
    if data.get("kind") == "CronJob":
        out.node(owner, "Job", source, by="helm")


def _ui_api(out: _Collect, repo: str, path: str, text: str) -> None:
    owner = _owner(out, repo)
    for url in re.findall(r"https?://[^'\"\s]+", text):
        host = _host(url)
        if owner and host:
            out.edge(
                owner,
                "calls",
                f"service:{host.split('.')[0]}",
                f"git:{repo}/{path}",
                by="ui_api_base",
            )


def _pyspark(out: _Collect, repo: str, path: str, text: str) -> None:
    owner = _owner(out, repo)
    if owner is None or "SparkSession" not in text:
        return
    source = f"git:{repo}/{path}"
    options = _OPTION.findall(text)
    database = next((v for k, v in options if k == "sfDatabase"), None)
    for k, v in options:
        if k == "subscribe":
            for topic in v.split(","):
                out.node(f"topic:{topic}", "Topic", source, tech=["kafka"], by="pyspark")
                out.edge(owner, "consumes", f"topic:{topic}", source, by="pyspark")
        elif k == "kafka.bootstrap.servers" and (m := re.search(r"b-\d+\.([a-z0-9-]+)\.kafka", v)):
            cluster = f"runtime:kafka/{m.group(1)}"
            out.node(cluster, "Runtime", source, tech=["kafka"], by="pyspark")
            for kk, vv in options:
                if kk == "subscribe":
                    for topic in vv.split(","):
                        out.edge(f"topic:{topic}", "part_of", cluster, source, by="pyspark")
        elif k == "sfWarehouse":
            wh = f"runtime:snowflake/{v.lower()}"
            out.node(wh, "Runtime", source, tech=["snowflake"], by="pyspark")
            out.edge(owner, "uses", wh, source, by="pyspark")
    for match in re.finditer(r'"dbtable"\s*,\s*"([A-Za-z0-9_.]+)"', text):
        table = match.group(1)
        if "." not in table and database:
            table = f"{database}.{table}"
        before = text[: match.start()]
        writing = max(before.rfind(".write"), before.rfind("writeStream")) > max(
            before.rfind(".read"), before.rfind("readStream")
        )
        node = f"table:{table.lower()}"
        out.node(node, "Datastore", source, tech=["snowflake"], by="pyspark")
        out.edge(owner, "writes" if writing else "reads", node, source, by="pyspark")


def _emr(out: _Collect, repo: str, path: str, text: str) -> None:
    owner = _owner(out, repo)
    data = yaml.safe_load(text) or {}
    if owner and isinstance(data, dict) and data.get("cluster"):
        runtime = f"runtime:emr/{data['cluster']}"
        out.node(runtime, "Runtime", f"git:{repo}/{path}", tech=["emr", "spark"], by="emr")
        out.edge(owner, "runs_on", runtime, f"git:{repo}/{path}", by="emr")


def _connector(out: _Collect, repo: str, path: str, text: str) -> None:
    owner = _owner(out, repo)
    config = json.loads(text).get("config", {})
    if owner is None:
        return
    source = f"git:{repo}/{path}"
    database = str(config.get("snowflake.database.name", "")).lower()
    for topic in str(config.get("topics", "")).split(","):
        if topic:
            out.node(f"topic:{topic}", "Topic", source, tech=["kafka"], by="kafka_connect")
            out.edge(owner, "consumes", f"topic:{topic}", source, by="kafka_connect")
    for pair in str(config.get("snowflake.topic2table.map", "")).split(","):
        if ":" in pair and database:
            table = f"table:{database}.{pair.split(':', 1)[1].lower()}"
            out.node(table, "Datastore", source, tech=["snowflake"], by="kafka_connect")
            out.edge(owner, "writes", table, source, by="kafka_connect")
    if dlq := config.get("errors.deadletterqueue.topic.name"):
        out.node(f"topic:{dlq}", "Topic", source, tech=["kafka"], by="kafka_connect")
        out.edge(owner, "publishes", f"topic:{dlq}", source, by="kafka_connect")


def _snowflake_sql(out: _Collect, repo: str, path: str, text: str) -> None:
    source = f"git:{repo}/{path}"
    for warehouse in re.findall(r"WAREHOUSE\s+([A-Z0-9_]+)\s+SET", text, re.I):
        out.node(
            f"runtime:snowflake/{warehouse.lower()}",
            "Runtime",
            source,
            tech=["snowflake"],
            by="snowflake_sql",
        )
    for statement in re.split(r";\s*\n", text):
        task = _TASK.search(statement)
        if task is None:
            continue
        owner = f"job:{task.group(1).lower()}"
        if wh := _WAREHOUSE.search(statement):
            runtime = f"runtime:snowflake/{wh.group(1).lower()}"
            out.node(runtime, "Runtime", source, tech=["snowflake"], by="snowflake_sql")
            out.edge(owner, "runs_on", runtime, source, by="snowflake_sql")
        body = statement[task.end() :]
        for m in re.finditer(r"\b(INTO|FROM|JOIN)\s+([A-Z0-9_]+\.[A-Z0-9_]+)", body, re.I):
            if m.group(2).upper().startswith(task.group(1).upper()):
                continue
            table = f"table:{m.group(2).lower()}"
            out.node(table, "Datastore", source, tech=["snowflake"], by="snowflake_sql")
            out.edge(
                owner,
                "writes" if m.group(1).upper() == "INTO" else "reads",
                table,
                source,
                by="snowflake_sql",
            )


def _airflow(out: _Collect, repo: str, path: str, text: str) -> None:
    if "from airflow" not in text:
        return
    source = f"git:{repo}/{path}"
    targets = [f"job:{t.lower()}" for t in _TASK_REF.findall(text)]
    targets += [f"job:{n}" for n in re.findall(r'"Name"\s*:\s*"([a-z0-9-]+)"', text)]
    for target in targets:
        out.edge(target, "scheduled_by", "runtime:airflow/*", source, by="airflow_dag")


def _streamlit(out: _Collect, repo: str, path: str, text: str) -> None:
    owner = _owner(out, repo)
    if owner is None or "import streamlit" not in text:
        return
    source = f"git:{repo}/{path}"
    if wh := re.search(r'warehouse\s*=\s*"([A-Za-z0-9_]+)"', text):
        runtime = f"runtime:snowflake/{wh.group(1).lower()}"
        out.node(runtime, "Runtime", source, tech=["snowflake"], by="streamlit")
        out.edge(owner, "uses", runtime, source, by="streamlit")
    for db, table in re.findall(r"FROM\s+([A-Z0-9_]+)\.([A-Z0-9_]+)", text):
        node = f"table:{db.lower()}.{table.lower()}"
        out.node(node, "Datastore", source, tech=["snowflake"], by="streamlit")
        out.edge(owner, "reads", node, source, by="streamlit")


_RELATIONS: list[tuple[Callable[[str], bool], Callable[[_Collect, str, str, str], None]]] = [
    (lambda p: re.search(r"(^|/)application[^/]*\.ya?ml$", p) is not None, _spring_config),
    (lambda p: "/helm/" in f"/{p}" and p.endswith((".yaml", ".yml")), _helm),
    (lambda p: re.search(r"environment[^/]*\.ts$", p) is not None, _ui_api),
    (lambda p: p.endswith(".py") and "/dags/" not in f"/{p}", _pyspark),
    (lambda p: p.endswith(".py") and "/dags/" not in f"/{p}", _streamlit),
    (lambda p: p.endswith("emr.yaml"), _emr),
    (lambda p: p.endswith("connector.json"), _connector),
    (lambda p: p.endswith(".sql"), _snowflake_sql),
    (lambda p: "/dags/" in f"/{p}" and p.endswith(".py"), _airflow),
]


# --- the CMDB and live discovery ---------------------------------------------------------


def _cmdb(
    out: _Collect, cmdb: Mapping[str, Any], declared: Mapping[str, NodeTemplate | None]
) -> None:
    installed: dict[str, str] = {}
    for ci in cmdb.get("cis") or []:
        if str(ci.get("install_status", "")).lower() == "retired":
            continue
        name, cls, sys_id = str(ci["name"]), str(ci.get("sys_class_name")), str(ci["sys_id"])
        source = f"cmdb:{sys_id}"
        if cls in _CI_RUNTIME:
            node = f"runtime:{name}"
            out.node(node, "Runtime", source, tech=["kubernetes"], by="cmdb")
        elif cls in _CI_KIND:
            kind = _CI_KIND[cls]
            node = f"database:{name.lower()}" if kind == "Database" else f"cmdb:{name}"
            if kind == "Database":
                out.node(node, "Database", source, tech=["oracle"], by="cmdb")
            else:
                out.node(node, kind, source, by="cmdb")
        elif (template := declared.get(cls)) is not None:  # ADR-0016: a plugin's CMDB class
            node = template.id.format(name=_slug(name))
            out.node(node, template.kind, source, tech=template.tech, by="cmdb")
        else:
            node = f"cmdb:{name}"  # resolved to a known runtime by name, or dropped
        installed[name] = node
    for rel in cmdb.get("relationships") or []:
        parent, child = installed.get(str(rel.get("parent"))), installed.get(str(rel.get("child")))
        if parent is None or child is None:
            continue  # a retired or unknown CI
        relation = str(rel.get("type", ""))
        edge_kind = "runs_on" if relation.startswith("Runs on") else "depends_on"
        out.edge(parent, edge_kind, child, f"cmdb:{rel['sys_id']}", by="cmdb")


def _live(
    out: _Collect, live: Mapping[str, Any], discovery: Sequence[tuple[str, str, Discovery]]
) -> None:
    """Layer 2: each live result through the rules its capability's plugin declares."""
    rules = {capability: (plugin, spec) for plugin, capability, spec in discovery}
    for key, result in live.items():
        capability, _, target = str(key).partition("@")
        if not isinstance(result, dict) or capability not in rules:
            continue
        plugin, spec = rules[capability]
        by = spec.by or f"live_{plugin}"
        source = f"live:{key}"
        root = {"target": target, "result": result}
        for rule in spec.rules:
            for env in _bindings(rule.each, root):
                if rule.node is not None:
                    node_id = _fill(rule.node.id, env)
                    if node_id is not None:
                        out.node(
                            node_id,
                            rule.node.kind,
                            source,
                            layer=2,
                            tech=list(rule.node.tech),
                            by=by,
                        )
                elif rule.edge is not None:
                    src, relation, dst = rule.edge
                    a, b = _fill(src, env), _fill(dst, env)
                    if a is not None and b is not None:
                        out.edge(a, relation, b, source, layer=2, by=by)


@dataclass(frozen=True, slots=True)
class _Entry:
    """One entry of a mapping a discovery rule iterates."""

    key: object
    value: object


def _read(path: str, env: Mapping[str, object]) -> object:
    head, *rest = path.split(".")
    value: object = env.get(head)
    for segment in rest:
        if isinstance(value, _Entry) and segment in ("key", "value"):
            value = getattr(value, segment)
        elif isinstance(value, dict):
            value = value.get(segment)
        else:
            return None
    return value


def _bindings(each: Mapping[str, str], env: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    """Every combination of ``each``'s bindings, in order (nested iteration)."""
    if not each:
        yield env
        return
    (name, path), *rest = each.items()
    value = _read(path, env)
    items: Iterable[object] = (
        [_Entry(k, v) for k, v in value.items()]
        if isinstance(value, dict)
        else value
        if isinstance(value, list)
        else []
    )
    for item in items:
        yield from _bindings(dict(rest), {**env, name: item})


def _fill(template: str, env: Mapping[str, object]) -> str | None:
    """The template with each placeholder read and filtered; ``None`` if one can't be."""
    missing = False

    def one(match: re.Match[str]) -> str:
        nonlocal missing
        path, *filters = (part.strip() for part in match.group(1).split("|"))
        value = _read(path, env)
        if value is None or isinstance(value, (dict, list, _Entry)):
            missing = True
            return ""
        text = str(value)
        for f in filters:
            name, _, arg = f.partition(":")
            if name == "lower":
                text = text.lower()
            elif name == "after_last":
                text = text.rsplit(arg, 1)[-1]
            elif name == "strip_prefix":
                text = text.removeprefix(arg)
        return text

    filled = PLACEHOLDER.sub(one, template)
    return None if missing or not filled else filled


# --- reconciling names ---------------------------------------------------------------------


def _reconcile(out: _Collect) -> None:
    """Resolve placeholder and CMDB names to the ids code and discovery use.

    A placeholder that resolves becomes an alias; one that doesn't (a CMDB CI
    with no counterpart, such as a cloud account) is dropped with its edges.
    """
    nodes = {f.id for f in out.facts if isinstance(f, NodeFact)}
    known = sorted(i for i in nodes if not _placeholder(i))
    by_name: dict[str, list[str]] = {}
    for node_id in known:
        by_name.setdefault(node_id.split(":", 1)[1], []).append(node_id)
        by_name.setdefault(node_id.rsplit("/", 1)[-1].split(":", 1)[-1], []).append(node_id)
    refs = {r for f in out.facts for r in _refs(f) if _placeholder(r)}
    resolved = {r: t for r in sorted(refs) if (t := _resolve(r, known, by_name)) is not None}
    out.facts = [
        f for f in out.facts if all(r in resolved or not _placeholder(r) for r in _refs(f))
    ]
    kinds = {f.id: f.kind for f in out.facts if isinstance(f, NodeFact) and not _placeholder(f.id)}
    for ref, target in resolved.items():
        source = next(f.source for f in out.facts if ref in _refs(f))
        name = ref.split(":", 1)[1]
        prefix = target.split(":", 1)[0]
        if (
            ref.startswith("cmdb:")
            and prefix in ("service", "job", "ui")
            and target != f"{prefix}:{name}"
        ):
            # The CMDB's name is the one incidents carry: it becomes the canonical id.
            out.alias(target, f"{prefix}:{name}", source, by="reconcile")
            out.alias(ref, f"{prefix}:{name}", source, by="reconcile")
            kinds[f"{prefix}:{name}"] = kinds.get(target, "Service")
            out.node(f"{prefix}:{name}", kinds[target], source, by="reconcile")
        else:
            out.alias(ref, target, source, by="reconcile")
    _type_cmdb_edges(out, resolved, kinds)


def _type_cmdb_edges(out: _Collect, resolved: Mapping[str, str], kinds: Mapping[str, str]) -> None:
    """A CMDB "Depends on" means calls (a service), uses (a database) or scheduled_by (Airflow)."""
    typed: list[Fact] = []
    for fact in out.facts:
        kept: Fact = fact
        if isinstance(fact, EdgeFact) and fact.extractor == "cmdb" and fact.kind == "depends_on":
            target = resolved.get(fact.dst, fact.dst)
            kind = kinds.get(target, "")
            meaning = (
                "calls"
                if kind in ("Service", "UI")
                else "uses"
                if kind == "Database"
                else "scheduled_by"
                if target.startswith("runtime:airflow/")
                else None
            )
            if meaning is not None:
                kept = EdgeFact(
                    fact.src, meaning, fact.dst, fact.source, fact.layer, fact.extractor
                )
        typed.append(kept)
    out.facts = typed


def _placeholder(ref: str) -> bool:
    return ref.startswith(("cmdb:", "*:")) or "/*" in ref


def _resolve(ref: str, known: list[str], by_name: Mapping[str, list[str]]) -> str | None:
    if ref.startswith("runtime:*/"):
        ns = ref.split("/", 1)[1]
        hits = [k for k in known if k.startswith("runtime:") and k.endswith(f"/{ns}")]
        return hits[0] if len(hits) == 1 else None
    if ref == "runtime:airflow/*":
        hits = [k for k in known if k.startswith("runtime:airflow/")]
        return hits[0] if len(hits) == 1 else None
    name = ref.split(":", 1)[1]
    exact = [k for k in by_name.get(name, []) if not k.startswith("cmdb:")]
    if len(set(exact)) == 1:
        return exact[0]
    words = set(re.split(r"[-_ ]+", name.lower())) - {""}
    loose = {
        k
        for k in known
        if k.split(":", 1)[0] in ("service", "job", "ui")
        and words <= set(re.split(r"[-_ ]+", k.split(":", 1)[1]))
    }
    return next(iter(loose)) if len(loose) == 1 else None


def _refs(fact: Fact) -> tuple[str, ...]:
    if isinstance(fact, NodeFact):
        return (fact.id,)
    if isinstance(fact, EdgeFact):
        return (fact.src, fact.dst)
    return ()


def _host(value: str) -> str | None:
    """The host of a URL, or a bare ``host.domain.tld`` value."""
    text = value.strip()
    if m := _URL_HOST.match(text):
        return m.group(1).lower()
    if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+){2,}", text.lower()):
        return text.lower()
    return None


def _internal(host: str) -> bool:
    return host.endswith((".internal", ".local", ".svc", ".cluster.local")) or "." not in host


def _leaves(data: object, prefix: str = "") -> Iterable[tuple[str, object]]:
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _leaves(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(data, list):
        for item in data:
            yield from _leaves(item, prefix)
    else:
        yield prefix, data


def mapped_cmdb_classes(declared: Sequence[tuple[str, Extractor]]) -> frozenset[str]:
    """Every CMDB class something turns into a node: the core's and the plugins' (ADR-0017)."""
    return (
        frozenset(_CI_KIND)
        | frozenset(_CI_RUNTIME)
        | frozenset(e.match.removeprefix("cmdb:") for _, e in declared if e.node is not None)
    )


__all__ = ["mapped_cmdb_classes", "structured_facts"]
