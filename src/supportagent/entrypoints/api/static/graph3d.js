// The system explorer (/ui/system): the learned graph in 3D, with drill-down.
// Served from /static (CSP: script-src 'self'); three.js r147 is vendored beside it (MIT).
//
// Key elements first: the components and the things two or more of them share. Selecting
// a node flies to it and unfolds everything it touches; the side panel becomes its file.
// Every string from the graph is set with textContent, never parsed as HTML.
(function () {
  "use strict";
  const root = document.querySelector(".explorer");
  if (!root) return;
  const stage = root.querySelector(".stage");
  const view = stage.querySelector(".viewport");
  const canvas = view.querySelector("canvas.scene");
  const labelLayer = stage.querySelector(".labels");
  const dossier = root.querySelector("[data-dossier]");
  const intro = Array.from(dossier.childNodes).map((n) => n.cloneNode(true));
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function flat(message) {
    const note = stage.querySelector(".no-webgl");
    note.hidden = false;
    if (message) note.textContent = message;
    canvas.hidden = true;
  }
  if (!window.THREE || !THREE.OrbitControls) return flat();
  try {
    const probe = document.createElement("canvas");
    if (!(probe.getContext("webgl2") || probe.getContext("webgl"))) return flat();
  } catch (_) {
    return flat();
  }

  // --- colours come from the stylesheet, so light and dark both hold --------------------
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  let palette = {};
  function readPalette() {
    palette = {
      ink: css("--ink"), ink2: css("--ink-2"), muted: css("--muted"), line: css("--line-2"),
      seal: css("--seal"), agent: css("--agent"), select: css("--select"), panel: css("--panel"), warn: css("--warn"),
      service: css("--k-service"), job: css("--k-job"), data: css("--k-data"),
      topic: css("--k-topic"), external: css("--k-external"),
    };
  }
  readPalette();

  function family(node) {
    const id = node.id, kind = (node.kind || "").toLowerCase();
    if (id.startsWith("service:") || id.startsWith("ui:") || kind === "service" || kind === "ui") return "service";
    if (id.startsWith("job:") || kind === "job") return "job";
    if (/topic|queue|stream|channel/.test(kind) || id.startsWith("topic:")) return "topic";
    if (/data|table|schema|store|bucket|cache|file|volume/.test(kind) || /^(table|database|schema|db):/.test(id)) return "data";
    return "external";
  }

  // --- the scene ---------------------------------------------------------------------
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 1, 6000);
  const home = new THREE.Vector3(0, 260, 620);
  camera.position.copy(home);
  const controls = new THREE.OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 60;
  controls.maxDistance = 2400;
  controls.autoRotate = !reduce;
  controls.autoRotateSpeed = 0.35;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 0.95));
  const sun = new THREE.DirectionalLight(0xffffff, 0.55);
  sun.position.set(200, 400, 300);
  scene.add(sun);
  const floorY = -120;
  let floor = null;
  function buildFloor() {
    if (floor) scene.remove(floor);
    floor = new THREE.PolarGridHelper(520, 12, 6, 64, palette.line, palette.line);
    floor.position.y = floorY;
    floor.material.transparent = true;
    floor.material.opacity = 0.55;
    scene.add(floor);
  }
  buildFloor();

  const GEOMETRY = {
    service: new THREE.SphereGeometry(1, 32, 20),
    job: new THREE.OctahedronGeometry(1.25, 0),
    data: new THREE.CylinderGeometry(0.95, 0.95, 1.5, 28),
    topic: new THREE.TorusGeometry(0.9, 0.34, 14, 40),
    external: new THREE.BoxGeometry(1.4, 1.4, 1.4),
  };
  const ringGeometry = new THREE.TorusGeometry(1.75, 0.09, 8, 64);

  // --- data --------------------------------------------------------------------------
  const nodes = new Map();
  const edges = [];
  const around = new Map(); // id -> [{edge, other, out}]
  let scope = "key";
  let trail = []; // the drill path, outermost first
  let selected = null;
  let hovered = null;
  let alpha = 1;

  const INVERSE = {
    publishes: "published by", consumes: "consumed by", writes: "written by", reads: "read by",
    uses: "used by", calls: "called by", depends_on: "needed by", runs_on: "hosts",
    part_of: "has part", scheduled_by: "schedules",
  };
  const verb = (kind, out) => (out ? kind.replace(/_/g, " ") : INVERSE[kind] || kind.replace(/_/g, " ") + " (in)");

  function addNode(data) {
    const fam = family(data);
    const isKey = data.component;
    const size = data.component ? 13 : 8;
    const material = new THREE.MeshLambertMaterial({ color: palette[fam] });
    if (data.status === "proposed") material.wireframe = true; // state is line form, not a fade
    const mesh = new THREE.Mesh(GEOMETRY[fam], material);
    mesh.scale.setScalar(size);
    if (fam === "topic") mesh.rotation.x = Math.PI / 2;
    mesh.userData.id = data.id;
    const halo = new THREE.Mesh(ringGeometry, new THREE.MeshBasicMaterial({ color: palette.seal }));
    halo.rotation.x = Math.PI / 2;
    halo.scale.setScalar(size);
    halo.visible = data.open > 0;
    const pick = new THREE.Mesh(ringGeometry, new THREE.MeshBasicMaterial({ color: palette.select }));
    pick.rotation.x = Math.PI / 2;
    pick.scale.setScalar(size * 1.25);
    pick.visible = false;
    const group = new THREE.Group();
    group.add(mesh, halo, pick);
    group.visible = false;
    scene.add(group);
    const label = document.createElement("div");
    label.className = "label hidden" + (data.component ? "" : " minor") + (data.status === "proposed" ? " proposed" : "");
    const name = document.createElement("span");
    name.textContent = data.name;
    label.appendChild(name);
    if (data.open > 0) {
      const open = document.createElement("span");
      open.className = "open";
      open.textContent = data.open + " open";
      label.appendChild(open);
    }
    label.addEventListener("click", () => select(data.id));
    labelLayer.appendChild(label);
    const angle = Math.random() * Math.PI * 2, r = 60 + Math.random() * 120;
    nodes.set(data.id, {
      data, fam, isKey, size, group, mesh, halo, pick, label,
      pos: new THREE.Vector3(Math.cos(angle) * r, (Math.random() - 0.5) * 60, Math.sin(angle) * r),
      vel: new THREE.Vector3(), shown: false, degree: 0,
    });
  }

  function addEdge(e) {
    const a = nodes.get(e.src), b = nodes.get(e.dst);
    if (!a || !b || a === b) return;
    const proposed = e.status === "proposed";
    const material = proposed
      ? new THREE.LineDashedMaterial({ color: palette.muted, dashSize: 6, gapSize: 5, transparent: true, opacity: 0.85 })
      : new THREE.LineBasicMaterial({ color: palette.ink2, transparent: true, opacity: 0.7 });
    const geometry = new THREE.BufferGeometry().setFromPoints([a.pos.clone(), b.pos.clone()]);
    const line = new THREE.Line(geometry, material);
    line.visible = false;
    scene.add(line);
    const edge = { a, b, kind: e.kind, proposed, line, shown: false };
    edges.push(edge);
    (around.get(a.data.id) || around.set(a.data.id, []).get(a.data.id)).push({ edge, other: b, out: true });
    (around.get(b.data.id) || around.set(b.data.id, []).get(b.data.id)).push({ edge, other: a, out: false });
  }

  // --- case paths: each open case's alert, and the hop triage made from it to a component ---
  // This is the agent's own path, so it alone is violet, and it never changes the system's edges.
  const paths = [];
  let showPaths = true;
  const alertGeometry = new THREE.ConeGeometry(7, 14, 4);
  const up = new THREE.Vector3(0, 1, 0);
  function addPath(c, more) {
    const n = nodes.get(c.node);
    if (!n) return;
    const marker = new THREE.Mesh(alertGeometry, new THREE.MeshLambertMaterial({ color: palette.seal }));
    marker.rotation.x = Math.PI; // points down at what it is about
    const hop = new THREE.Mesh(new THREE.CylinderGeometry(1.4, 1.4, 1, 8), new THREE.MeshBasicMaterial({ color: palette.agent }));
    scene.add(marker, hop);
    const label = document.createElement("a");
    label.className = "label alert";
    label.href = "/ui/cases/" + encodeURIComponent(c.id);
    if (c.severity) { const sev = el("span", { class: "sev sev-" + c.severity }, c.severity); label.appendChild(sev); }
    label.appendChild(el("span", {}, String(c.alert).replace(/^webhook:/, "") || c.label));
    if (more) label.appendChild(el("span", { class: "more" }, "+" + more + " more"));
    labelLayer.appendChild(label);
    if (c.exhibits) {
      const tag = el("span", { class: "tag" }, c.held + "/" + c.exhibits + " held");
      tag.title = "RCA exhibits on this case: " + c.held + " of " + c.exhibits + " checks held";
      n.label.appendChild(tag);
    }
    paths.push({ c, n, marker, hop, label, at: new THREE.Vector3() });
  }
  function placePaths() {
    paths.forEach((p) => {
      const on = showPaths && p.n.shown;
      p.marker.visible = p.hop.visible = on;
      if (!on) { p.label.classList.add("hidden"); return; }
      p.at.copy(p.n.pos).add(new THREE.Vector3(0, p.n.size * 2 + 46, 0));
      p.marker.position.copy(p.at);
      const from = p.at.clone().add(new THREE.Vector3(0, -7, 0)), to = p.n.pos.clone().add(new THREE.Vector3(0, p.n.size * 1.05, 0));
      const mid = from.clone().add(to).multiplyScalar(0.5), len = from.distanceTo(to);
      p.hop.position.copy(mid);
      p.hop.scale.set(1, Math.max(len, 1), 1);
      p.hop.quaternion.setFromUnitVectors(up, to.clone().sub(from).normalize());
    });
  }

  function load(graph) {
    graph.nodes.forEach(addNode);
    graph.edges.forEach(addEdge);
    const perNode = new Map();
    // One marker per part: its most pressing case (the server sends them most severe first).
    (graph.cases || []).forEach((c) => perNode.set(c.node, (perNode.get(c.node) || []).concat([c])));
    perNode.forEach((list) => addPath(list[0], list.length - 1));
    const pathToggle = root.querySelector("[data-paths]");
    if (pathToggle) {
      pathToggle.hidden = !paths.length;
      pathToggle.addEventListener("click", () => {
        showPaths = !showPaths;
        pathToggle.setAttribute("aria-pressed", String(showPaths));
        needs = true;
      });
    }
    // A thing two components share (a topic both touch, a table both write) is a key element.
    nodes.forEach((n) => {
      const touching = new Set((around.get(n.data.id) || []).filter((l) => l.other.data.component).map((l) => l.other.data.id));
      n.degree = (around.get(n.data.id) || []).length;
      if (!n.data.component && touching.size >= 2) n.isKey = true;
    });
    if (![...nodes.values()].some((n) => n.isKey)) nodes.forEach((n) => (n.isKey = true));
    refresh(true);
    // Settle the layout before the first frame, so the model opens already framed.
    for (let i = 0; i < 600 && alpha >= 0.003; i++) tick();
    framed = true;
    frame(true);
    animate();
  }

  // --- what is shown -------------------------------------------------------------------
  function refresh(reheat) {
    const show = new Set();
    nodes.forEach((n, id) => { if (scope === "all" || n.isKey) show.add(id); });
    trail.forEach((id) => {
      show.add(id);
      (around.get(id) || []).forEach((l) => show.add(l.other.data.id));
    });
    nodes.forEach((n, id) => {
      const on = show.has(id);
      if (on && !n.shown) {
        // A node that unfolds starts beside the node it unfolded from.
        const from = trail.length ? nodes.get(trail[trail.length - 1]) : null;
        if (from && from !== n) n.pos.copy(from.pos).add(new THREE.Vector3((Math.random() - 0.5) * 30, (Math.random() - 0.5) * 30, (Math.random() - 0.5) * 30));
      }
      n.shown = on;
      n.group.visible = on;
    });
    edges.forEach((e) => { e.shown = e.a.shown && e.b.shown; e.line.visible = e.shown; });
    if (reheat) alpha = Math.max(alpha, 0.6);
    paintSelection();
  }

  function related(id) {
    const set = new Set([id]);
    (around.get(id) || []).forEach((l) => set.add(l.other.data.id));
    return set;
  }

  function paintSelection() {
    const focus = selected ? related(selected) : null;
    nodes.forEach((n, id) => {
      n.pick.visible = id === selected;
      const dim = focus && !focus.has(id);
      n.mesh.material.opacity = dim ? 0.22 : 1;
      n.mesh.material.transparent = !!dim;
      n.label.classList.toggle("selected", id === selected);
      n.label.classList.toggle("dim", !!dim);
    });
    edges.forEach((e) => {
      const hot = selected && (e.a.data.id === selected || e.b.data.id === selected);
      e.line.material.color.set(hot ? palette.select : e.proposed ? palette.muted : palette.ink2);
      e.line.material.opacity = focus && !hot ? 0.15 : hot ? 1 : 0.7;
    });
    needs = true;
  }

  // --- layout: a small force simulation (repulsion, springs, a pull to the bench) ---------
  function tick() {
    if (alpha < 0.003) return;
    const shown = [...nodes.values()].filter((n) => n.shown);
    const force = new THREE.Vector3();
    for (let i = 0; i < shown.length; i++) {
      const a = shown[i];
      for (let j = i + 1; j < shown.length; j++) {
        const b = shown[j];
        force.subVectors(a.pos, b.pos);
        let d2 = force.lengthSq();
        if (d2 < 1) { force.set(Math.random() - 0.5, Math.random() - 0.5, Math.random() - 0.5); d2 = 1; }
        if (d2 < 25) d2 = 25;
        force.multiplyScalar((90000 * alpha) / (d2 * Math.sqrt(d2)));
        a.vel.add(force);
        b.vel.sub(force);
      }
    }
    edges.forEach((e) => {
      if (!e.shown) return;
      force.subVectors(e.b.pos, e.a.pos);
      const d = force.length() || 1;
      const rest = e.a.data.component && e.b.data.component ? 150 : 95;
      force.multiplyScalar(((d - rest) / d) * 0.04 * alpha);
      e.a.vel.add(force);
      e.b.vel.sub(force);
    });
    shown.forEach((n) => {
      n.vel.x -= n.pos.x * 0.004 * alpha;
      n.vel.z -= n.pos.z * 0.004 * alpha;
      n.vel.y -= n.pos.y * 0.009 * alpha; // wider than tall: a model on a bench, not a ball
      n.vel.multiplyScalar(0.82);
      n.pos.add(n.vel);
    });
    alpha *= 0.985;
    if (!framed && alpha < 0.08) { framed = true; if (!trail.length) frame(); }
    needs = true;
  }

  // Fit what is shown: centre on it and back off until it all fits the view.
  let framed = false;
  function frame(now) {
    const shown = [...nodes.values()].filter((n) => n.shown);
    if (!shown.length) return;
    const box = new THREE.Box3();
    shown.forEach((n) => box.expandByPoint(n.pos));
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const fov = (camera.fov * Math.PI) / 180;
    const fit = Math.min(fov, 2 * Math.atan(Math.tan(fov / 2) * camera.aspect));
    const dist = Math.max(220, (sphere.radius * 0.82 + 30) / Math.tan(fit / 2));
    const dir = new THREE.Vector3(0, 0.5, 1).normalize();
    floor.scale.setScalar(Math.max(0.4, (sphere.radius * 1.25) / 520));
    floor.position.set(sphere.center.x, box.min.y - 50, sphere.center.z);
    let to = sphere.center.clone().add(dir.clone().multiplyScalar(dist));
    // Refine on what the camera would actually see: scale so the farthest node sits inside 80%.
    const probe = camera.clone();
    probe.position.copy(to);
    probe.lookAt(sphere.center);
    probe.updateMatrixWorld();
    let reach = 0;
    shown.forEach((n) => { const q = n.pos.clone().project(probe); reach = Math.max(reach, Math.abs(q.x), Math.abs(q.y)); });
    if (reach > 0) to = sphere.center.clone().add(dir.clone().multiplyScalar(Math.max(160, dist * (reach / 0.8))));
    if (now) { controls.target.copy(sphere.center); camera.position.copy(to); needs = true; }
    else fly(sphere.center, to);
  }

  function place() {
    nodes.forEach((n) => n.group.position.copy(n.pos));
    placePaths();
    edges.forEach((e) => {
      if (!e.shown) return;
      const p = e.line.geometry.attributes.position;
      p.setXYZ(0, e.a.pos.x, e.a.pos.y, e.a.pos.z);
      p.setXYZ(1, e.b.pos.x, e.b.pos.y, e.b.pos.z);
      p.needsUpdate = true;
      e.line.geometry.computeBoundingSphere();
      if (e.proposed) e.line.computeLineDistances();
    });
  }

  // --- labels: HTML over the canvas, so they stay crisp and themed ------------------------
  const v = new THREE.Vector3();
  function rank(n) {
    if (n.data.id === selected) return 0;
    if (n.data.open > 0) return 1;
    if (selected && related(selected).has(n.data.id)) return 2;
    return n.data.component ? 3 : 4;
  }
  const head = stage.querySelector(".stage-head");
  function clear(box, w, h) {
    const top = head && getComputedStyle(head).position === "absolute" ? head.offsetHeight : 0;
    return box[0] >= 4 && box[2] <= w - 4 && box[1] >= top && box[3] <= h - 4;
  }
  function placeLabels() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const focus = selected ? related(selected) : null;
    const placed = [];
    const ordered = [...nodes.entries()].sort((a, b) => rank(a[1]) - rank(b[1]));
    paths.forEach((p) => {
      if (!showPaths || !p.n.shown) return;
      v.copy(p.at).project(camera);
      if (v.z > 1) { p.label.classList.add("hidden"); return; }
      const x = (v.x * 0.5 + 0.5) * w, y = (-v.y * 0.5 + 0.5) * h - 12;
      p.label.classList.remove("hidden");
      p.label.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px) translate(-50%,-100%)";
      const lw = p.label.offsetWidth, lh = p.label.offsetHeight;
      const box = [x - lw / 2, y - lh, x + lw / 2, y];
      if (!clear(box, w, h) || placed.some((q) => box[0] < q[2] && box[2] > q[0] && box[1] < q[3] && box[3] > q[1])) {
        p.label.classList.add("hidden");
        return;
      }
      placed.push(box);
    });
    ordered.forEach(([id, n]) => {
      const roomy = w >= 640 || n.data.component; // a phone labels components until one is opened
      const wanted = n.shown && ((roomy && (n.isKey || scope === "all")) || (focus && focus.has(id)) || id === hovered || trail.includes(id));
      v.copy(n.pos);
      v.y += n.size * 1.6;
      v.project(camera);
      const behind = v.z > 1;
      if (!wanted || behind) { n.label.classList.add("hidden"); return; }
      const x = (v.x * 0.5 + 0.5) * w, y = (-v.y * 0.5 + 0.5) * h;
      if (!n.lw) { n.label.classList.remove("hidden"); n.lw = n.label.offsetWidth; n.lh = n.label.offsetHeight; }
      const box = [x - n.lw / 2, y - n.lh, x + n.lw / 2, y];
      const covers = placed.some((q) => box[0] < q[2] + 2 && box[2] > q[0] - 2 && box[1] < q[3] + 1 && box[3] > q[1] - 1);
      if ((covers && rank(n) > 1) || (!clear(box, w, h) && rank(n) > 0)) { n.label.classList.add("hidden"); return; }
      placed.push(box);
      n.label.classList.remove("hidden");
      n.label.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px) translate(-50%,-100%)";
    });
  }

  // --- camera flight: the one authored motion ----------------------------------------------
  let flight = null;
  // A flight to a node aims at where the node is now, every frame, then keeps following it
  // while its unfolded neighbours settle.
  let follow = null;
  function fly(toTarget, toCamera, node) {
    follow = null;
    const offset = toCamera.clone().sub(toTarget);
    if (reduce) { controls.target.copy(toTarget); camera.position.copy(toCamera); follow = node ? { node, offset } : null; needs = true; return; }
    flight = { t0: performance.now(), ms: 900, fromT: controls.target.clone(), fromC: camera.position.clone(), toT: toTarget.clone(), offset, node };
  }
  function stepFlight(now) {
    if (flight) {
      const k = Math.min(1, (now - flight.t0) / flight.ms);
      const e = k === 1 ? 1 : 1 - Math.pow(2, -10 * k); // exponential ease-out
      const toT = flight.node ? flight.node.pos : flight.toT;
      controls.target.lerpVectors(flight.fromT, toT, e);
      camera.position.lerpVectors(flight.fromC, toT.clone().add(flight.offset), e);
      if (k === 1) { follow = flight.node ? { node: flight.node, offset: flight.offset } : null; flight = null; }
      needs = true;
    } else if (follow && alpha >= 0.003) {
      const delta = follow.node.pos.clone().sub(controls.target);
      controls.target.add(delta);
      camera.position.add(delta);
      needs = true;
    }
  }
  canvas.addEventListener("pointerdown", () => { follow = null; flight = null; });

  // --- selection and the dossier -------------------------------------------------------
  function select(id, fromTrail) {
    const n = nodes.get(id);
    if (!n) return;
    controls.autoRotate = false;
    selected = id;
    if (fromTrail) trail = trail.slice(0, trail.indexOf(id) + 1);
    else if (!trail.includes(id)) trail.push(id);
    refresh(true);
    // Settle the unfolded nodes a little first, so the fit sees where they land.
    for (let i = 0; i < 120 && alpha >= 0.003; i++) tick();
    let reach = 60;
    (around.get(id) || []).forEach((l) => { reach = Math.max(reach, l.other.pos.distanceTo(n.pos)); });
    const fov = (camera.fov * Math.PI) / 180;
    const fit = Math.min(fov, 2 * Math.atan(Math.tan(fov / 2) * camera.aspect));
    const dist = Math.max(180, (reach + 30) / Math.tan(fit / 2));
    const dir = camera.position.clone().sub(controls.target).normalize();
    fly(n.pos.clone(), n.pos.clone().add(dir.multiplyScalar(dist)), n);
    renderDossier(n);
  }

  function back() {
    if (!trail.length) return;
    trail.pop();
    if (trail.length) select(trail[trail.length - 1], true);
    else reset();
  }

  function reset() {
    trail = [];
    selected = null;
    refresh(true);
    frame();
    dossier.replaceChildren(...intro.map((x) => x.cloneNode(true)));
  }

  function el(tag, attrs, text) {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, val]) => (k === "class" ? (e.className = val) : e.setAttribute(k, val)));
    if (text != null) e.textContent = text;
    return e;
  }

  function renderDossier(n) {
    const d = n.data;
    const out = [];
    const path = el("nav", { class: "path", "aria-label": "Drill path" });
    const top = el("button", { type: "button" }, "System");
    top.addEventListener("click", reset);
    path.appendChild(top);
    trail.forEach((id) => {
      path.appendChild(el("span", { "aria-hidden": "true" }, "›"));
      const b = el("button", { type: "button" }, nodes.get(id).data.name);
      b.addEventListener("click", () => select(id, true));
      path.appendChild(b);
    });
    out.push(path);
    out.push(el("h2", {}, d.name));
    const chips = el("div", { class: "chips" });
    chips.appendChild(el("span", { class: "chip" }, d.kind));
    if (d.status === "proposed") chips.appendChild(el("span", { class: "chip proposed" }, "proposed"));
    if (d.running === true) chips.appendChild(el("span", { class: "badge ok" }, "running"));
    if (d.running === false) chips.appendChild(el("span", { class: "badge" }, "defined, not running"));
    (d.tech || []).forEach((t) => chips.appendChild(el("span", { class: "chip" }, t)));
    out.push(chips);
    if (d.gist) out.push(el("p", { class: "gist" }, d.gist));
    if (d.open > 0) {
      const alarm = el("div", { class: "alarm" });
      alarm.appendChild(el("strong", {}, d.open + " open case" + (d.open === 1 ? "" : "s")));
      alarm.appendChild(document.createTextNode(" on this component. "));
      alarm.appendChild(el("a", { href: "/ui/cases?service=" + encodeURIComponent(d.id.split(":").slice(1).join(":")) }, "Open the queue"));
      out.push(alarm);
    }
    const facts = el("dl", { class: "facts-grid" });
    const groups = Object.entries(d.groups || {});
    if (groups.length) {
      facts.appendChild(el("dt", {}, "What it does"));
      facts.appendChild(el("dd", {}, groups.map(([g, c]) => c + " " + g).join(" · ")));
    }
    if (d.cases) { facts.appendChild(el("dt", {}, "Cases")); facts.appendChild(el("dd", {}, d.cases + " in all")); }
    if ((d.sources || []).length) {
      facts.appendChild(el("dt", {}, "Learned from"));
      const dd = el("dd");
      d.sources.forEach((s, i) => { if (i) dd.appendChild(document.createTextNode(", ")); dd.appendChild(el("code", {}, s)); });
      facts.appendChild(dd);
    }
    if (facts.childNodes.length) out.push(facts);
    const links = (around.get(d.id) || []).slice().sort((a, b) => Number(b.other.data.component) - Number(a.other.data.component) || a.other.data.name.localeCompare(b.other.data.name));
    out.push(el("h3", {}, "Connections (" + links.length + ")"));
    if (links.length) {
      const ul = el("ul", { class: "links" });
      links.forEach((l) => {
        const li = el("li", { class: l.edge.proposed ? "proposed" : "" });
        const b = el("button", { type: "button" });
        b.appendChild(el("span", { class: "verb" }, verb(l.edge.kind, l.out)));
        b.appendChild(el("span", {}, l.other.data.name));
        b.appendChild(el("span", { class: "chip" }, l.other.data.kind));
        b.addEventListener("click", () => select(l.other.data.id));
        b.addEventListener("mouseenter", () => { hovered = l.other.data.id; needs = true; });
        b.addEventListener("mouseleave", () => { hovered = null; needs = true; });
        li.appendChild(b);
        ul.appendChild(li);
      });
      out.push(ul);
    } else out.push(el("p", { class: "muted" }, "No connections are known yet."));
    if (d.component) {
      const acts = el("div", { class: "actions" });
      acts.appendChild(el("a", { class: "button primary", href: "/ui/system/component?id=" + encodeURIComponent(d.id) }, "Open its file"));
      acts.appendChild(el("a", { class: "button", href: "/ui/cases?service=" + encodeURIComponent(d.id.split(":").slice(1).join(":")) }, "Its cases"));
      out.push(acts);
    }
    dossier.replaceChildren(...out);
  }

  // --- input ---------------------------------------------------------------------------
  const ray = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let down = null;
  function hit(ev) {
    const r = canvas.getBoundingClientRect();
    pointer.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(pointer, camera);
    const meshes = [...nodes.values()].filter((n) => n.shown).map((n) => n.mesh);
    const found = ray.intersectObjects(meshes, false)[0];
    return found ? found.object.userData.id : null;
  }
  canvas.addEventListener("pointerdown", (ev) => { down = [ev.clientX, ev.clientY]; controls.autoRotate = false; });
  canvas.addEventListener("pointermove", (ev) => {
    const id = hit(ev);
    if (id !== hovered) { hovered = id; canvas.style.cursor = id ? "pointer" : ""; needs = true; }
  });
  canvas.addEventListener("pointerup", (ev) => {
    if (!down || Math.hypot(ev.clientX - down[0], ev.clientY - down[1]) > 5) return;
    const id = hit(ev);
    if (id) select(id);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !ev.target.closest("input, select, textarea")) back();
  });
  root.querySelectorAll("[data-scope]").forEach((b) => b.addEventListener("click", () => {
    scope = b.dataset.scope;
    framed = !!trail.length;
    root.querySelectorAll("[data-scope]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    refresh(true);
  }));
  root.querySelector("[data-reset]").addEventListener("click", reset);
  const find = root.querySelector("[data-find]");
  const list = document.getElementById("graph-nodes");
  function findNode(text) {
    const q = text.trim().toLowerCase();
    if (!q) return null;
    let best = null;
    nodes.forEach((n) => { const name = n.data.name.toLowerCase(); if (name === q) best = n; else if (!best && name.includes(q)) best = n; });
    return best;
  }
  find.addEventListener("change", () => { const n = findNode(find.value); if (n) { select(n.data.id); find.value = ""; } });
  find.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); find.dispatchEvent(new Event("change")); } });

  // --- frame loop ------------------------------------------------------------------------
  let needs = true;
  function resize() {
    const w = view.clientWidth, h = view.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
    needs = true;
  }
  new ResizeObserver(resize).observe(view);
  resize();
  controls.addEventListener("change", () => (needs = true));
  function animate(now) {
    requestAnimationFrame(animate);
    if (document.hidden) return;
    tick();
    stepFlight(now || performance.now());
    if (controls.autoRotate) needs = true;
    controls.update();
    if (!needs) return;
    place();
    placeLabels();
    renderer.render(scene, camera);
    needs = false;
  }
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    readPalette();
    buildFloor();
    nodes.forEach((n) => { n.mesh.material.color.set(palette[n.fam]); n.halo.material.color.set(palette.seal); n.pick.material.color.set(palette.select); });
    paths.forEach((p) => { p.marker.material.color.set(palette.seal); p.hop.material.color.set(palette.agent); });
    paintSelection();
  });

  fetch(root.dataset.graph, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
    .then((graph) => {
      if (!graph.nodes.length) return flat("Nothing to draw yet.");
      if (list) list.replaceChildren(...graph.nodes.map((n) => el("option", { value: n.name })));
      load(graph);
      const wanted = new URLSearchParams(location.search).get("focus");
      if (wanted && nodes.has(wanted)) select(wanted);
    })
    .catch(() => flat("The graph didn't load. The components are listed below."));
})();
