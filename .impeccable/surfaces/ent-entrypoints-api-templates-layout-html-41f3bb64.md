---
version: 1
slug: "ent-entrypoints-api-templates-layout-html-41f3bb64"
primary_target: "src/supportagent/entrypoints/api/templates/layout.html"
related_targets: ["src/supportagent/entrypoints/api/templates/system.html","src/supportagent/entrypoints/api/templates/case.html","src/supportagent/entrypoints/api/templates/trace.html"]
---

## Scope

The whole admin UI shell (layout.html + app.css) and its three lead surfaces: System (3D graph), Case (decision custody chain), Trace (agent decision replay and log evidence). Mode: Operate. Audience: every role equally; a newcomer must be able to triage from it.

## Direction contract

THESIS: Every decision is an exhibit: tagged, sealed, and its custody chain one click away. Refuses the category default of KPI tiles over a grey card grid with one blue accent.

OWN-WORLD: Lab-bench blue-grey ground (#eef1f4) and deep ink navy rail (#16202d); exhibit-tag yellow only on evidence IDs; seal red only on approvals, P1 and failures; one reserved violet ink for the agent's path. Dark: graphite lightbox, same tag/seal/violet. State is line form as well as hue: confirmed solid, proposed dashed, stale half-weight, did-not-run struck. Notched exhibit tags in monospace; custody rows numbered in violet rings.

STORY: The visitor sees the system as a 3D model, sees which component a case sits on and the violet path the agent took, opens the case, reads each step of the chain with its exhibits, and approves, rejects or records the outcome. J/K moves to the next case.

FIRST VIEWPORT: Left ink rail (brand, nav, open-case and approval counts, who/mode). Centre: 3D system graph on a bench grid, nodes sized by kind, open cases ringed red, exhibit tags pinned; toolbar All / Case path / Flows / reset. Right 432px panel: selected node or case, custody chain, the seal action.

FORM: Evidence Room, position 4 of 7 on the grounded list; seed:8d23aa7a11a53bab. Raises: orienteering (one violet path layer that never edits the system), emission-line rail (state as line form), vertical feed (triage mode J/K), lexicon (rank by apparatus: supported claims carry tags, speculation none).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Memorable moment

Clicking a node flies the camera to it, unfolds its neighbours and detail facts as child nodes, and the side panel turns into that component's exhibit file.

## Unresolved

Live Loki browsing from the UI is a new read path (needs an ADR); this build shows log evidence the RCA and trace already captured.
