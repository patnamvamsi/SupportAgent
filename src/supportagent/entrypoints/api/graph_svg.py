"""The system graph as an SVG for the review page (ADR-0015 decision 4: easy, visually).

A layered drawing: one column per kind of thing, left to right as requests
flow (UIs, then services and jobs, then topics and datastores, then
databases, then runtimes and third parties). A confirmed edge is a solid
line, a proposed one dashed, a rejected one isn't drawn. Each node links to
the page focused on it, which filters the fact cards beside the drawing; the
focused node's edges are emphasised. Server-rendered, no script needed.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote

from markupsafe import Markup, escape

from supportagent.app.graph_review import FactView

_COLUMNS = {
    "UI": 0,
    "Service": 1,
    "Job": 1,
    "Topic": 2,
    "Datastore": 2,
    "Database": 3,
    "Runtime": 4,
    "External": 4,
}
_COL_W, _ROW_H, _BOX_W, _BOX_H, _PAD = 230, 40, 190, 26, 20
_LABEL_MAX = 26


def render(facts: Sequence[FactView], *, focus: str | None = None) -> Markup:
    nodes = {f.src: f for f in facts if f.fact == "node" and f.status != "rejected"}
    edges = [f for f in facts if f.fact == "edge" and f.status != "rejected" and f.dst is not None]
    for edge in edges:  # an edge's ends are drawn even when only the edge was proposed
        for end in (edge.src, edge.dst):
            if end is not None and end not in nodes:
                nodes[end] = _placeholder(end)
    columns: dict[int, list[str]] = {}
    for node_id, node in sorted(nodes.items()):
        columns.setdefault(_COLUMNS.get(node.kind, 4), []).append(node_id)
    pos = {
        node_id: (_PAD + col * _COL_W, _PAD + row * _ROW_H)
        for col, ids in columns.items()
        for row, node_id in enumerate(ids)
    }
    width = _PAD * 2 + (max(columns, default=0) + 1) * _COL_W
    height = _PAD * 2 + max((len(v) for v in columns.values()), default=1) * _ROW_H
    parts = [
        f'<svg class="graph" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        'role="img" aria-label="System graph: confirmed edges solid, proposed dashed">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" '
        'fill="currentColor"/></marker></defs>',
        *(_edge(e, pos, focus) for e in edges if e.src in pos and e.dst in pos),
        *(_node(nodes[n], xy, focus) for n, xy in pos.items()),
        "</svg>",
    ]
    return Markup("".join(parts))  # noqa: S704  # nosec B704 — every value is escaped


def _placeholder(node_id: str) -> FactView:
    return FactView(node_id, "node", node_id, "Service", None, (), (), 0, "proposed", True, None)


def _edge(edge: FactView, pos: dict[str, tuple[int, int]], focus: str | None) -> str:
    (x1, y1), (x2, y2) = pos[edge.src], pos[str(edge.dst)]
    sy, ty = y1 + _BOX_H // 2, y2 + _BOX_H // 2
    if x2 <= x1:  # same column or backwards: loop around the right side
        sx, tx = x1 + _BOX_W, x2 + _BOX_W
        mid = max(sx, tx) + 40
    else:
        sx, tx = x1 + _BOX_W, x2
        mid = (sx + tx) // 2
    d = f"M {sx} {sy} C {mid} {sy}, {mid} {ty}, {tx} {ty}"
    classes = ["edge", edge.status] + (["focus"] if focus in (edge.src, edge.dst) else [])
    return (
        f'<path class="{escape(" ".join(classes))}" d="{d}" marker-end="url(#arrow)">'
        f"<title>{escape(edge.text)} ({escape(edge.status)})</title></path>"
    )


def _node(node: FactView, xy: tuple[int, int], focus: str | None) -> str:
    x, y = xy
    label = node.src.split(":", 1)[1]
    short = label if len(label) <= _LABEL_MAX else label[: _LABEL_MAX - 2] + "…"
    classes = ["node", node.kind.lower(), node.status] + (["focus"] if node.src == focus else [])
    href = f"/ui/onboarding/graph?focus={quote(node.src)}#cards"
    title = f"{node.src} · {node.kind} · {node.status}"
    return (
        f'<a href="{escape(href)}"><g class="{escape(" ".join(classes))}">'
        f'<rect x="{x}" y="{y}" width="{_BOX_W}" height="{_BOX_H}" rx="6"/>'
        f'<text x="{x + 10}" y="{y + 17}">{escape(short)}</text>'
        f"<title>{escape(title)}</title></g></a>"
    )


__all__ = ["render"]
