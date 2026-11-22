"""The code layer's Python analyser (ADR-0023 §3, WP-221): library facts from source.

Each module is parsed with :mod:`ast`; nothing is imported or run. Plugins
declare what to look for (:class:`~supportagent.domain.model.plugin.CodeRule`):
a call (``producer.send(TOPIC, ...)``), a decorator (``@celery_app.task``), an
assignment (``__tablename__ = "orders"``), a string literal (SQL) or an import.

A rule's value is resolved statically, per repository, the way a reader would:

- string literals, ``a + b`` and ``x if c else y`` of resolvable parts;
- a name: a local of the enclosing function first (a parameter is unknown),
  then a module or class constant in the same module, then anywhere in the
  repository when only one value is bound to that name (``from app.config
  import TOPIC``, ``settings.topic``, ``self.topic`` set in ``__init__``);
- ``os.getenv("X", "default")`` and ``os.environ.get(...)``: the default;
- a call to one of the repository's own functions whose every ``return``
  resolves (``self._topic_for(symbol)``); ``list(...)``, ``set(...)`` and
  comprehensions: their items;
- an f-string: its literal parts, with ``*`` for what can't be resolved
  (``f"ticks.{symbol}"`` is ``ticks.*``); a value that is only ``*`` is dropped.
  A ``string`` rule reads an f-string whole, so ``f"SELECT … FROM {table}"``
  yields the table's pattern;
- a list, tuple or set: each item.

Bare string statements (docstrings) are never read as SQL. A module that
doesn't parse (Python 2, templates) is skipped and counted.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from supportagent.domain.model.plugin import CodeRule

_ENV_GETTERS = frozenset({"getenv", "get"})
_WRAPPERS = frozenset({"list", "set", "tuple", "sorted", "frozenset", "str"})
_MAX_VALUES = 20  # a list larger than this is data, not configuration
_MAX_DEPTH = 8
_RESOLVE_PASSES = 4
_MAX_VALUE_LEN = 200


@dataclass(frozen=True, slots=True)
class CodeFinding:
    """One rule match: ``path`` is ``<repo>/<path>``, ``value`` resolved (may hold ``*``)."""

    plugin: str
    rule: CodeRule
    path: str
    line: int
    value: str
    groups: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CodeReport:
    findings: list[CodeFinding] = field(default_factory=list)
    modules: int = 0
    unparsed: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Table:
    """What the repository binds: constants per module and by bare name, and returns."""

    local: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    by_name: dict[str, set[str]] = field(default_factory=dict)
    returns: dict[str, set[str]] = field(default_factory=dict)
    local_returns: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    def bind(self, module: str, name: str, values: Iterable[str]) -> bool:
        found = set(values)
        if not found:
            return False
        before = len(self.local.get((module, name), ())), len(self.by_name.get(name, ()))
        self.local.setdefault((module, name), set()).update(found)
        self.by_name.setdefault(name, set()).update(found)
        return (len(self.local[(module, name)]), len(self.by_name[name])) != before

    def bind_return(self, module: str, name: str, values: Iterable[str]) -> bool:
        found = set(values)
        before = len(self.returns.get(name, ())), len(self.local_returns.get((module, name), ()))
        self.returns.setdefault(name, set()).update(found)
        self.local_returns.setdefault((module, name), set()).update(found)
        after = len(self.returns[name]), len(self.local_returns[(module, name)])
        return after != before

    def returned(self, module: str, name: str) -> list[str] | None:
        """What a function returns: one in the same module, else the repository's."""
        for values in (self.local_returns.get((module, name)), self.returns.get(name)):
            if values:
                return sorted(values)
        return None

    def lookup(self, module: str, name: str) -> list[str] | None:
        for values in (self.local.get((module, name)), self.by_name.get(name)):
            if values is not None and len(values) == 1:
                return sorted(values)
        return None


@dataclass(frozen=True, slots=True)
class _Scope:
    """A function's parameters (unknown) and local assignments."""

    params: frozenset[str] = frozenset()
    locals: Mapping[str, tuple[ast.expr, ...]] = field(default_factory=dict)

    @classmethod
    def of(cls, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> _Scope:
        args = fn.args
        params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        params |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
        assigned: dict[str, list[ast.expr]] = {}
        for node in _own_nodes(fn):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, []).append(node.value)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    assigned.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                params |= _names(node.target)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                params |= _names(node.optional_vars)
        return cls(frozenset(params), {k: tuple(v) for k, v in assigned.items()})

    def unknown(self, names: Iterable[str]) -> _Scope:
        return _Scope(self.params | set(names), self.locals)


def python_findings(files: Mapping[str, str], rules: Sequence[tuple[str, CodeRule]]) -> CodeReport:
    """Every match of ``rules`` (``(plugin id, rule)``) in the ``.py`` files of ``files``.

    ``files`` maps ``<repo>/<path>`` to text, as the onboarding workspace holds it.
    """
    report = CodeReport()
    if not rules:
        return report
    by_repo: dict[str, dict[str, ast.Module]] = {}
    for key, text in sorted(files.items()):
        if not key.endswith(".py"):
            continue
        try:
            tree = ast.parse(text, filename=key)
        except (SyntaxError, ValueError):
            report.unparsed.append(key)
            continue
        by_repo.setdefault(key.partition("/")[0], {})[key] = tree
        report.modules += 1
    for modules in by_repo.values():
        table = _bindings(modules)
        for key, tree in modules.items():
            imported = _imports(tree)
            active = [
                (plugin, rule)
                for plugin, rule in rules
                if not rule.imports or imported & set(rule.imports)
            ]
            if active:
                matcher = _Matcher(key, table, active, _docstrings(tree))
                matcher.visit(tree, None)
                report.findings.extend(matcher.found)
    return report


# --- what the repository binds ----------------------------------------------------------


def _bindings(modules: Mapping[str, ast.Module]) -> _Table:
    table = _Table()
    for _ in range(_RESOLVE_PASSES):  # a binding defined from another resolves next pass
        changed = False
        for key, tree in modules.items():
            for name, expr, scope in _constant_sites(tree):
                values = _Resolver(key, table, scope).resolve(expr)
                if values is not None:
                    changed |= table.bind(key, name, values)
            for fn in (n for n in ast.walk(tree) if isinstance(n, _FUNCTIONS)):
                values = _returns(fn, key, table)
                if values:
                    changed |= table.bind_return(key, fn.name, values)
        if not changed:
            break
    return table


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _constant_sites(tree: ast.Module) -> Iterator[tuple[str, ast.expr, _Scope | None]]:
    """``NAME = expr`` at module and class level, and ``self.NAME = expr`` in methods."""
    bodies: list[list[ast.stmt]] = [tree.body]
    bodies.extend(n.body for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    for body in bodies:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        yield target.id, stmt.value, None
            elif (
                isinstance(stmt, ast.AnnAssign)
                and stmt.value is not None
                and isinstance(stmt.target, ast.Name)
            ):
                yield stmt.target.id, stmt.value, None
    for fn in (n for n in ast.walk(tree) if isinstance(n, _FUNCTIONS)):
        scope = _Scope.of(fn)
        for node in _own_nodes(fn):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        yield target.attr, node.value, scope


def _returns(fn: ast.FunctionDef | ast.AsyncFunctionDef, key: str, table: _Table) -> list[str]:
    """The values ``fn`` returns, when every ``return`` resolves (else none)."""
    scope = _Scope.of(fn)
    values: list[str] = []
    returns = [n for n in _own_nodes(fn) if isinstance(n, ast.Return)]
    for ret in returns:
        if ret.value is None:
            return []
        found = _Resolver(key, table, scope).resolve(ret.value)
        if found is None:
            return []
        values.extend(found)
    return values


def _own_nodes(fn: ast.AST) -> Iterator[ast.AST]:
    """``fn``'s nodes, not descending into nested functions, classes or lambdas."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_FUNCTIONS, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _names(target: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


class _Resolver:
    def __init__(self, module: str, table: _Table, scope: _Scope | None) -> None:
        self._module = module
        self._table = table
        self._scope = scope
        self._depth = 0

    def resolve(self, expr: ast.expr) -> list[str] | None:
        """The string values ``expr`` can take, or ``None`` when it can't be resolved."""
        if self._depth > _MAX_DEPTH:
            return None
        self._depth += 1
        try:
            values = self._resolve(expr)
        finally:
            self._depth -= 1
        return values[:_MAX_VALUES] if values else None

    def _resolve(self, expr: ast.expr) -> list[str] | None:  # noqa: PLR0911 — one case per expression kind
        if isinstance(expr, ast.Constant):
            return [expr.value] if isinstance(expr.value, str) else None
        if isinstance(expr, ast.Name):
            return self._name(expr.id)
        if isinstance(expr, ast.Attribute):
            return self._table.lookup(self._module, expr.attr)
        if isinstance(expr, ast.JoinedStr):
            return self._fstring(expr)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left, right = self.resolve(expr.left), self.resolve(expr.right)
            if left is None or right is None:
                return None
            return [a + b for a in left for b in right]
        if isinstance(expr, ast.IfExp):
            return self._union([expr.body, expr.orelse])
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            return None if len(expr.elts) > _MAX_VALUES else self._union(expr.elts)
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            inner = _Resolver(self._module, self._table, self._comprehension(expr))
            inner._depth = self._depth
            return inner.resolve(expr.elt)
        if isinstance(expr, ast.Call):
            return self._call(expr)
        return None

    def _name(self, name: str) -> list[str] | None:
        scope = self._scope
        if scope is not None and name in scope.params:
            return None
        if scope is not None and name in scope.locals:
            return self._union(list(scope.locals[name]))
        return self._table.lookup(self._module, name)

    def _call(self, call: ast.Call) -> list[str] | None:
        callee = _callee(call)
        if callee in _ENV_GETTERS and _is_env(call):
            default = call.args[1] if len(call.args) > 1 else _keyword(call, "default")
            return self.resolve(default) if default is not None else None
        if callee in _WRAPPERS and len(call.args) == 1:
            return self.resolve(call.args[0])
        return self._table.returned(self._module, callee) if callee is not None else None

    def _comprehension(self, expr: ast.ListComp | ast.SetComp | ast.GeneratorExp) -> _Scope:
        targets = {n for gen in expr.generators for n in _names(gen.target)}
        return (self._scope or _Scope()).unknown(targets)

    def _union(self, exprs: Sequence[ast.expr]) -> list[str] | None:
        values = [v for e in exprs if (vs := self.resolve(e)) for v in vs]
        return values or None

    def _fstring(self, expr: ast.JoinedStr) -> list[str] | None:
        outs = [""]
        for part in expr.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                outs = [o + part.value for o in outs]
            elif isinstance(part, ast.FormattedValue):
                values = self.resolve(part.value) or ["*"]
                outs = [o + v for o in outs for v in values][:_MAX_VALUES]
        found = [re.sub(r"\*+", "*", o) for o in outs]
        kept = [o for o in dict.fromkeys(found) if o.strip("*. _-/:") != ""]
        return kept or None


def _is_env(call: ast.Call) -> bool:
    """``os.getenv(...)``, ``getenv(...)``, ``os.environ.get(...)``, ``environ.get(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "getenv"
    if not isinstance(func, ast.Attribute):
        return False
    owner = func.value
    if func.attr == "getenv":
        return isinstance(owner, ast.Name) and owner.id == "os"
    return (isinstance(owner, ast.Name) and owner.id == "environ") or (
        isinstance(owner, ast.Attribute) and owner.attr == "environ"
    )


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module.partition(".")[0])
    return found


def _docstrings(tree: ast.Module) -> set[int]:
    """Literals not read alone: bare string statements (docstrings, commented-out
    SQL), and the parts of an f-string (the f-string is read whole)."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
            skip.add(id(node.value))
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
    return skip


def _callee(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


# --- matching -----------------------------------------------------------------------


class _Matcher:
    def __init__(
        self,
        key: str,
        table: _Table,
        rules: Sequence[tuple[str, CodeRule]],
        skip: set[int],
    ) -> None:
        self._key = key
        self._table = table
        self._rules = [(p, r, re.compile(r.name)) for p, r in rules]
        self._skip = skip
        self.found: list[CodeFinding] = []

    def visit(self, node: ast.AST, scope: _Scope | None) -> None:
        if isinstance(node, _FUNCTIONS):
            self._check(node, scope)
            scope = _Scope.of(node)
            for child in ast.iter_child_nodes(node):
                self.visit(child, scope)
            return
        self._check(node, scope)
        for child in ast.iter_child_nodes(node):
            self.visit(child, scope)

    def _check(self, node: ast.AST, scope: _Scope | None) -> None:
        resolver = _Resolver(self._key, self._table, scope)
        for plugin, rule, name in self._rules:
            for line, values in self._candidates(node, rule, name, resolver):
                for value in values:
                    self.found.extend(_finding(plugin, rule, self._key, line, value))

    def _candidates(  # noqa: PLR0912 — one case per construct
        self, node: ast.AST, rule: CodeRule, name: re.Pattern[str], resolver: _Resolver
    ) -> Iterator[tuple[int, list[str]]]:
        if rule.match == "call" and isinstance(node, ast.Call):
            callee = _callee(node)
            if callee is None or not name.fullmatch(callee) or not _receiver_ok(node, rule):
                return
            arg = _argument(node, rule)
            if arg is None:
                return
            values = [n] if rule.callable and (n := _callee(arg)) else resolver.resolve(arg)
            if values:
                yield node.lineno, values
        elif rule.match == "decorator" and isinstance(node, _FUNCTIONS):
            for deco in node.decorator_list:
                callee = _callee(deco)
                if callee is None or not name.fullmatch(callee):
                    continue
                if rule.arg is None and rule.kwarg is None:
                    yield node.lineno, [node.name]
                elif (
                    isinstance(deco, ast.Call)
                    and (arg := _argument(deco, rule)) is not None
                    and (values := resolver.resolve(arg))
                ):
                    yield node.lineno, values
        elif rule.match == "assign" and isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [_callee(t) for t in targets]
            if (
                node.value is not None
                and any(n and name.fullmatch(n) for n in names)
                and (values := resolver.resolve(node.value))
            ):
                yield node.lineno, values
        elif rule.match == "string" and id(node) not in self._skip:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield node.lineno, [node.value]
            elif isinstance(node, ast.JoinedStr) and (values := resolver.resolve(node)):
                yield node.lineno, values
        elif rule.match == "import" and isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if node.module and not node.level
                else []
            )
            tops = [m.partition(".")[0] for m in modules if name.fullmatch(m.partition(".")[0])]
            if tops:
                yield node.lineno, tops


def _receiver_ok(call: ast.Call, rule: CodeRule) -> bool:
    """With ``receiver``, a method call on an object whose expression matches it."""
    if rule.receiver is None:
        return True
    if not isinstance(call.func, ast.Attribute):
        return False
    return re.search(rule.receiver, ast.unparse(call.func.value)) is not None


def _argument(call: ast.Call, rule: CodeRule) -> ast.expr | None:
    if rule.kwarg is not None and (found := _keyword(call, rule.kwarg)) is not None:
        return found
    if rule.arg is not None and rule.arg < len(call.args):
        arg = call.args[rule.arg]
        return None if isinstance(arg, ast.Starred) else arg
    return None


def _finding(plugin: str, rule: CodeRule, key: str, line: int, value: str) -> Iterator[CodeFinding]:
    pattern = re.compile(rule.value)
    if rule.match == "string":
        matches: Iterable[re.Match[str]] = pattern.finditer(value)
    else:
        whole = pattern.fullmatch(value)
        matches = [whole] if whole else []
    for match in matches:
        groups = {k: v for k, v in match.groupdict().items() if v}
        found = groups.get("value", match.group(0))
        if found.strip("*") and len(found) <= _MAX_VALUE_LEN:
            yield CodeFinding(plugin, rule, key, line, found, {**groups, "value": found})


__all__ = ["CodeFinding", "CodeReport", "python_findings"]
