/* Панель «Свет по видео» (light_by_video): кадр, зоны, действия, детектор, журнал. */

const API = "/api/light_by_video";

const ROLE_LABEL = { motion: "движение", ignore: "исключить" };
const ROLE_COLOR = { motion: "#58a6ff", ignore: "#8b949e" };
const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

const h = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};

const fmt = (ts) => {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (e) {
    return ts;
  }
};

const fmtClock = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return iso;
  }
};

const pct = (v) => (v === undefined || v === null ? "—" : (Number(v) * 100).toFixed(2) + " %");

class LightByVideoPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._entry = null;
    this._state = null;
    this._tab = "zones";
    this._selected = null;
    this._dirty = false;
    this._started = false;
    this._frameUrl = null;
    this._drag = null;
    this._maskMode = false;
  }

  set hass(hass) {
    this._hass = hass;
    this._maybeStart();
    if (this._entriesSelect) this._syncEntries();
  }

  connectedCallback() {
    this._maybeStart();
  }

  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
    this._started = false;
    if (this._frameUrl) URL.revokeObjectURL(this._frameUrl);
  }

  _maybeStart() {
    if (this._started || !this._hass) return;
    this._started = true;
    this._render();
    this._tick();
    this._timer = setInterval(() => this._tick(), 2000);
  }

  // ------------------------------------------------------------------ API
  async _api(path, opts) {
    const resp = await this._hass.fetchWithAuth(API + path, opts);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.json();
  }

  _query() {
    return this._entry ? `?entry_id=${this._entry}` : "";
  }

  async _tick() {
    if (!this._hass || document.hidden) return;
    try {
      const join = this._query() ? "&" : "?";
      const data = await this._api("/state" + this._query() + join + "t=" + Date.now());
      this._state = data;
      if (!this._entry) this._entry = data.entry_id;
      this._paintStatus();
      if (this._tab === "journal") this._paintJournal();
      const zones = data.settings?.zones || [];
      const sig = JSON.stringify(zones.map((z) => [z.id, z.x, z.y, z.w, z.h, z.role, z.name]));
      if (sig !== this._zonesSig) {
        this._zonesSig = sig;
        this._paintZones();
      }
      this._paintZoneStates();
      this._paintObjects();
    } catch (err) {
      this._status("нет связи с интеграцией: " + err.message, true);
    }
    await this._loadFrame();
  }

  async _loadFrame() {
    try {
      const join = this._query() ? "&" : "?";
      const path = this._maskMode ? "/mask" : "/frame";
      // маску обновляем реже, чем кадр
      if (this._maskMode && Date.now() - (this._maskTs || 0) < 1500) return;
      const resp = await this._hass.fetchWithAuth(API + path + this._query() + join + "t=" + Date.now());
      if (!resp.ok) return;
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const img = this.shadowRoot.querySelector("#frame");
      const old = this._frameUrl;
      img.onload = () => {
        if (old) URL.revokeObjectURL(old);
      };
      img.src = url;
      this._frameUrl = url;
      if (this._maskMode) this._maskTs = Date.now();
    } catch (err) {
      /* кадр может быть недоступен — не шумим */
    }
  }

  // ------------------------------------------------------------------ каркас
  _render() {
    const root = this.shadowRoot;
    root.innerHTML = "";
    const style = document.createElement("style");
    style.textContent = `
      :host { display:block; padding:8px 12px 16px; color: var(--primary-text-color); }
      .top { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:8px; }
      .title { font-size:18px; font-weight:600; margin-right:6px; }
      .dot { width:9px; height:9px; border-radius:50%; background:#8b949e; display:inline-block; margin-right:5px; }
      .dot.ok { background:#3fb950; } .dot.bad { background:#f85149; }
      .status { font-size:13px; opacity:.85; }
      .body { display:flex; gap:12px; align-items:flex-start; flex-wrap:wrap; }
      .stage-wrap { flex:1 1 520px; min-width:320px; }
      .stage { position:relative; background:#000; border-radius:8px; overflow:hidden; line-height:0; user-select:none; }
      .stage img { width:100%; height:auto; display:block; }
      .overlay { position:absolute; inset:0; }
      .zone { position:absolute; border:2px solid #58a6ff; box-sizing:border-box; cursor:move; }
      .zone.ignore { border-style:dashed; border-color:#8b949e; }
      .zone.active { border-color:#f85149; box-shadow:0 0 0 2px rgba(248,81,73,.35) inset; }
      .zone .tag { position:absolute; left:0; top:-20px; font-size:11px; background:rgba(13,17,23,.85);
                   color:#e6edf3; padding:1px 6px; border-radius:4px; white-space:nowrap; }
      .zone .rs { position:absolute; width:12px; height:12px; background:#58a6ff; border-radius:3px; }
      .zone .rs.nw { left:-7px; top:-7px; cursor:nwse-resize; }
      .zone .rs.ne { right:-7px; top:-7px; cursor:nesw-resize; }
      .zone .rs.sw { left:-7px; bottom:-7px; cursor:nesw-resize; }
      .zone .rs.se { right:-7px; bottom:-7px; cursor:nwse-resize; }
      .zone .size { position:absolute; right:0; bottom:-18px; font-size:11px; white-space:nowrap;
                    background:rgba(13,17,23,.8); color:#e6edf3; padding:1px 4px; border-radius:4px; }
      .zone.selected { outline:1px solid #e6edf3; }
      .polys { position:absolute; inset:0; width:100%; height:100%; overflow:visible; pointer-events:none; }
      .polys polygon { fill:rgba(88,166,255,.10); stroke:#58a6ff; stroke-width:2px; vector-effect:non-scaling-stroke; }
      .polys polygon.ignore { stroke:#8b949e; fill:rgba(139,148,158,.10); stroke-dasharray:6 4; }
      .polys polygon.active { stroke:#f85149; fill:rgba(248,81,73,.14); }
      .vtx, .mid { position:absolute; width:12px; height:12px; margin:-6px 0 0 -6px; border-radius:50%;
                   background:#58a6ff; border:1px solid #0d1117; cursor:grab; pointer-events:auto; }
      .vtx:hover { background:#e6edf3; }
      .mid { width:9px; height:9px; margin:-4.5px 0 0 -4.5px; background:transparent; border:1px dashed #e6edf3;
             border-radius:2px; cursor:copy; opacity:.85; }
      .obj { position:absolute; border:1px dashed #f0c419; box-sizing:border-box; pointer-events:none; }
      .obj .otag { position:absolute; left:0; top:-16px; font-size:10px; background:rgba(13,17,23,.85);
                   color:#f0c419; padding:0 4px; border-radius:3px; white-space:nowrap; }
      .legend { font-size:12px; opacity:.7; margin-top:6px; }
      .side { flex:0 0 380px; min-width:300px; }
      .tabs { display:flex; gap:4px; flex-wrap:wrap; margin-bottom:8px; }
      .tabs button { padding:6px 10px; border-radius:6px; border:1px solid var(--divider-color,#354354);
                     background:transparent; color:inherit; cursor:pointer; }
      .tabs button.on { background:#1f6feb; border-color:#1f6feb; color:#fff; }
      .row { display:flex; gap:8px; align-items:center; margin:6px 0; }
      .row label { flex:0 0 130px; font-size:13px; opacity:.85; }
      input, select { padding:6px; border-radius:6px; border:1px solid var(--divider-color,#354354);
                      background:var(--card-background-color,#161b22); color:inherit; min-width:0; }
      input[type=number] { width:90px; }
      .row input[type=text], .row select { flex:1 1 auto; }
      button.act { padding:6px 10px; border-radius:6px; border:1px solid var(--divider-color,#354354);
                   background:transparent; color:inherit; cursor:pointer; }
      button.act.primary { background:#238636; border-color:#238636; color:#fff; }
      .zlist { display:flex; flex-direction:column; gap:6px; }
      .zitem { display:flex; gap:8px; align-items:center; padding:6px 8px; border:1px solid var(--divider-color,#354354);
               border-radius:8px; cursor:pointer; font-size:13px; }
      .zitem.on { border-color:#1f6feb; background:rgba(31,111,235,.12); }
      .zitem .name { flex:1 1 auto; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .zitem .mini { opacity:.7; font-size:11px; }
      table { width:100%; border-collapse:collapse; font-size:12px; }
      td, th { padding:4px 6px; border-bottom:1px solid var(--divider-color,#30363d); text-align:left; vertical-align:top; }
      .muted { opacity:.6; font-size:12px; }
      .hint { opacity:.55; font-size:11px; line-height:1.35; margin:-2px 0 6px 130px; }
      .help h3 { margin:12px 0 4px; font-size:14px; }
      .help ul { margin:4px 0 8px 18px; padding:0; }
      .help li { margin:2px 0; font-size:12px; }
      .help code { background:rgba(127,127,127,.15); padding:0 3px; border-radius:3px; }
      @media (max-width: 720px) { .hint { margin-left: 0; } }
      .days { display:flex; gap:4px; flex-wrap:wrap; }
      .days label { display:flex; gap:2px; align-items:center; font-size:12px; }
      @media (max-width: 720px) { .side { flex:1 1 100%; } .row label { flex:0 0 110px; } }
    `;
    root.appendChild(style);

    const top = h("div", "top");
    top.append(
      h("span", "title", "Свет по видео"),
      this._el("span", "status", ""),
    );
    this._entriesSelect = document.createElement("select");
    this._entriesSelect.style.display = "none";
    this._entriesSelect.onchange = () => {
      this._entry = this._entriesSelect.value;
      this._tick();
    };
    top.append(this._entriesSelect);
    const btns = h("div", "row");
    btns.style.margin = "0";
    const bTest = h("button", "act", "Проверить сейчас");
    bTest.onclick = () => this._test();
    const bMask = h("button", "act", "Показать маску");
    bMask.onclick = () => {
      this._maskMode = !this._maskMode;
      bMask.textContent = this._maskMode ? "Показать кадр" : "Показать маску";
      this._maskTs = 0;
      this._loadFrame();
    };
    btns.append(bTest, bMask);
    top.append(btns);
    root.append(top);

    const body = h("div", "body");
    const stageWrap = h("div", "stage-wrap");
    const stage = h("div", "stage");
    stage.id = "stage";
    const img = document.createElement("img");
    img.id = "frame";
    img.alt = "кадр";
    const overlay = h("div", "overlay");
    overlay.id = "overlay";
    stage.append(img, overlay);
    stageWrap.append(stage, h("div", "legend", "Перетащите мышью по кадру — новая зона. Тяните зону, чтобы переместить; квадрат в углу — размер."));
    body.append(stageWrap);

    const side = h("div", "side");
    this._tabsEl = h("div", "tabs");
    side.append(this._tabsEl);
    this._pane = h("div", "pane");
    side.append(this._pane);
    body.append(side);
    root.append(body);

    this._statusEl = top.querySelector(".status");
    this._overlay = overlay;
    this._stage = stage;
    this._paintTabs();
    this._paintPane();
    this._bindStage();
  }

  _el(tag, cls, text) {
    const node = h(tag, cls, text);
    return node;
  }

  _paintTabs() {
    const tabs = [["zones", "Зоны"], ["action", "Действия и периоды"], ["detector", "Детектор"], ["interlocks", "Запреты"], ["source", "Источник"], ["journal", "Журнал"], ["help", "Справка"]];
    this._tabsEl.innerHTML = "";
    for (const [id, title] of tabs) {
      const b = h("button", this._tab === id ? "on" : "", title);
      b.onclick = () => {
        this._tab = id;
        this._paintTabs();
        this._paintPane();
      };
      this._tabsEl.append(b);
    }
  }

  _paintStatus() {
    if (!this._state || !this._statusEl) return;
    const st = this._state;
    const ok = st.camera_ok;
    this._statusEl.innerHTML = "";
    const dot = h("span", "dot " + (ok ? "ok" : "bad"));
    this._statusEl.append(
      dot,
      document.createTextNode(
        (ok ? "кадр идёт" : "источник недоступен") +
          " · " + (st.enabled ? "действия вкл" : "действия выкл") +
          (st.ir ? " · ИК" : "") +
          (st.frame ? " · " + st.frame : "") +
          (st.error ? " · " + st.error : "")
      )
    );
    this._syncEntries();
  }

  _syncEntries() {
    if (!this._entriesSelect || !this._hass) return;
    // список записей берём из устройства интеграции (по сущностям)
    const ids = new Set();
    Object.keys(this._hass.states).forEach((eid) => {
      if (eid.startsWith("switch.") && this._hass.states[eid].attributes?.actions !== undefined) ids.add(eid);
    });
    // проще: одна запись — селектор скрыт
    this._entriesSelect.style.display = "none";
  }

  // ------------------------------------------------------------------ зоны на кадре
  _bindStage() {
    const overlay = this._overlay;
    overlay.onmousedown = (ev) => {
      if (ev.target !== overlay) return;
      const rect = this._stage.getBoundingClientRect();
      const start = this._rel(ev, rect);
      this._dragging = true;
      this._drag = { mode: "new", start, id: null };
      ev.preventDefault();
    };
    window.addEventListener("mousemove", this._onMove = (ev) => this._dragMove(ev));
    window.addEventListener("mouseup", this._onUp = (ev) => this._dragEnd(ev));
  }

  _rel(ev, rect) {
    const x = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
    const y = Math.min(1, Math.max(0, (ev.clientY - rect.top) / rect.height));
    return { x, y };
  }

  _paintZoneStates() {
    if (!this._overlay || !this._state) return;
    const states = {};
    (this._state.zone_states || []).forEach((z) => (states[z.id] = z));
    this._overlay.querySelectorAll(".zone").forEach((node) => {
      const id = node.dataset.id;
      const st = states[id];
      node.classList.toggle("active", !!(st && st.active));
    });
  }

  _zonePoints(zone) {
    if (Array.isArray(zone.points) && zone.points.length >= 3) return zone.points.map((p) => [p[0], p[1]]);
    return [
      [zone.x, zone.y],
      [zone.x + zone.w, zone.y],
      [zone.x + zone.w, zone.y + zone.h],
      [zone.x, zone.y + zone.h],
    ];
  }

  _paintZones() {
    if (!this._overlay || this._dragging) return;
    this._overlay.innerHTML = "";
    const settings = this._state?.settings;
    if (!settings) return;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "polys");
    svg.setAttribute("viewBox", "0 0 1 1");
    svg.setAttribute("preserveAspectRatio", "none");
    this._overlay.append(svg);

    const states = {};
    (this._state.zone_states || []).forEach((z) => (states[z.id] = z));

    for (const zone of settings.zones) {
      const st = states[zone.id] || {};
      const isPoly = Array.isArray(zone.points) && zone.points.length >= 3;
      const cls = (zone.role === "ignore" ? " ignore" : "") + (st.active ? " active" : "");

      if (isPoly) {
        const poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
        poly.setAttribute("points", zone.points.map((pt) => `${pt[0]},${pt[1]}`).join(" "));
        poly.setAttribute("class", cls.trim());
        poly.dataset.id = zone.id;
        poly.style.pointerEvents = "auto";
        poly.onmousedown = (ev) => this._startMove(ev, zone);
        svg.append(poly);
        const label = h("div", "tag");
        label.textContent = `${zone.name}${st.ratio !== undefined ? " · " + pct(st.ratio) : ""}`;
        label.style.left = zone.x * 100 + "%";
        label.style.top = zone.y * 100 + "%";
        label.style.position = "absolute";
        label.style.pointerEvents = "none";
        this._overlay.append(label);
        if (zone.id === this._selected) this._paintVertices(zone);
        continue;
      }

      const node = h("div", "zone" + cls);
      node.dataset.id = zone.id;
      node.style.left = zone.x * 100 + "%";
      node.style.top = zone.y * 100 + "%";
      node.style.width = zone.w * 100 + "%";
      node.style.height = zone.h * 100 + "%";
      if (zone.role === "ignore") node.style.borderColor = ROLE_COLOR.ignore;
      const tag = h("span", "tag", `${zone.name}${st.ratio !== undefined ? " · " + pct(st.ratio) : ""}`);
      const size = h("span", "size", `${(zone.w * 100).toFixed(1)}×${(zone.h * 100).toFixed(1)} %`);
      node.append(tag, size);
      for (const dir of ["nw", "ne", "sw", "se"]) {
        const hd = h("span", "rs " + dir);
        hd.dataset.dir = dir;
        node.append(hd);
      }
      node.onmousedown = (ev) => this._startMove(ev, zone);
      this._overlay.append(node);
    }
    this._paintZoneSelection();
    this._paintObjects();
  }

  _startMove(ev, zone) {
    ev.stopPropagation();
    this._selected = zone.id;
    this._paintZoneSelection();
    const rect = this._stage.getBoundingClientRect();
    const start = this._rel(ev, rect);
    const dir = ev.target?.dataset?.dir || "";
    this._dragging = true;
    this._drag = { mode: dir ? "resize" : "move", dir, start, id: zone.id, orig: { ...zone } };
    ev.preventDefault();
  }

  _paintVertices(zone) {
    const pts = this._zonePoints(zone);
    pts.forEach((pt, idx) => {
      const v = h("div", "vtx");
      v.style.left = pt[0] * 100 + "%";
      v.style.top = pt[1] * 100 + "%";
      v.dataset.idx = idx;
      v.title = "тянуть — двигать вершину; правый клик — удалить";
      v.onmousedown = (ev) => {
        ev.stopPropagation();
        const rect = this._stage.getBoundingClientRect();
        this._dragging = true;
        this._drag = { mode: "vertex", id: zone.id, idx, start: this._rel(ev, rect), points: this._zonePoints(zone) };
        ev.preventDefault();
      };
      v.oncontextmenu = (ev) => {
        ev.preventDefault();
        if (pts.length <= 3) return;
        const next = pts.filter((_, i) => i !== idx);
        this._saveZone(zone.id, { points: next });
      };
      this._overlay.append(v);
    });
    // середины рёбер — вставить вершину
    pts.forEach((pt, idx) => {
      const nxt = pts[(idx + 1) % pts.length];
      const m = h("div", "mid");
      m.style.left = ((pt[0] + nxt[0]) / 2) * 100 + "%";
      m.style.top = ((pt[1] + nxt[1]) / 2) * 100 + "%";
      m.title = "нажмите — добавить вершину";
      m.onmousedown = (ev) => {
        ev.stopPropagation();
        const arr = this._zonePoints(zone);
        const nx = (arr[idx][0] + arr[(idx + 1) % arr.length][0]) / 2;
        const ny = (arr[idx][1] + arr[(idx + 1) % arr.length][1]) / 2;
        arr.splice(idx + 1, 0, [nx, ny]);
        const rect = this._stage.getBoundingClientRect();
        this._dragging = true;
        this._drag = { mode: "vertex", id: zone.id, idx: idx + 1, start: this._rel(ev, rect), points: arr };
        ev.preventDefault();
      };
      this._overlay.append(m);
    });
  }

  _paintObjects() {
    if (!this._overlay || this._dragging) return;
    this._overlay.querySelectorAll(".obj").forEach((n) => n.remove());
    const items = (this._state?.objects || {}).items || [];
    for (const it of items) {
      const box = it.box || [];
      if (box.length !== 4) continue;
      const node = h("div", "obj");
      node.style.left = box[0] * 100 + "%";
      node.style.top = box[1] * 100 + "%";
      node.style.width = box[2] * 100 + "%";
      node.style.height = box[3] * 100 + "%";
      node.append(h("span", "otag", `${it.name || ""} ${Math.round((it.conf || 0) * 100)}%`));
      this._overlay.append(node);
    }
  }

  _paintZoneSelection() {
    if (!this._overlay) return;
    this._overlay.querySelectorAll(".zone").forEach((n) => n.classList.toggle("selected", n.dataset.id === this._selected));
  }

  _dragMove(ev) {
    if (!this._drag || !this._stage) return;
    const rect = this._stage.getBoundingClientRect();
    const pos = this._rel(ev, rect);
    const d = this._drag;
    if (d.mode === "new") {
      const x = Math.min(d.start.x, pos.x);
      const y = Math.min(d.start.y, pos.y);
      const w = Math.abs(pos.x - d.start.x);
      const hh = Math.abs(pos.y - d.start.y);
      d.box = { x, y, w, h: hh };
      this._drawPreview(d.box);
      return;
    }
    if (d.mode === "vertex") {
      if (!d.points) {
        const zone = (this._state.settings.zones || []).find((z) => z.id === d.id) || {};
        d.points = this._zonePoints(zone);
      }
      d.points[d.idx] = [Math.min(1, Math.max(0, pos.x)), Math.min(1, Math.max(0, pos.y))];
      const poly = this._overlay.querySelector(`polygon[data-id="${d.id}"]`);
      if (poly) poly.setAttribute("points", d.points.map((pt) => `${pt[0]},${pt[1]}`).join(" "));
      this._overlay.querySelectorAll(".vtx").forEach((v) => {
        if (Number(v.dataset.idx) !== d.idx) return;
        v.style.left = d.points[d.idx][0] * 100 + "%";
        v.style.top = d.points[d.idx][1] * 100 + "%";
      });
      return;
    }
    const node = this._overlay.querySelector(`.zone[data-id="${d.id}"]`);
    if (!node) return;
    const MIN = 0.02;
    let { x, y, w, h: hh } = d.orig;
    const dx = pos.x - d.start.x;
    const dy = pos.y - d.start.y;
    if (d.mode === "move") {
      x = Math.min(1 - w, Math.max(0, d.orig.x + dx));
      y = Math.min(1 - hh, Math.max(0, d.orig.y + dy));
    } else {
      const dir = d.dir || "se";
      if (dir.includes("e")) w = Math.min(1 - x, Math.max(MIN, d.orig.w + dx));
      if (dir.includes("s")) hh = Math.min(1 - y, Math.max(MIN, d.orig.h + dy));
      if (dir.includes("w")) {
        const right = d.orig.x + d.orig.w;
        x = Math.max(0, Math.min(right - MIN, d.orig.x + dx));
        w = right - x;
      }
      if (dir.includes("n")) {
        const bottom = d.orig.y + d.orig.h;
        y = Math.max(0, Math.min(bottom - MIN, d.orig.y + dy));
        hh = bottom - y;
      }
    }
    node.style.left = x * 100 + "%";
    node.style.top = y * 100 + "%";
    node.style.width = w * 100 + "%";
    node.style.height = hh * 100 + "%";
    const size = node.querySelector(".size");
    if (size) size.textContent = `${(w * 100).toFixed(1)}×${(hh * 100).toFixed(1)} %`;
    d.current = { x, y, w, h: hh };
  }

  _drawPreview(box) {
    let node = this._overlay.querySelector(".zone.preview");
    if (!node) {
      node = h("div", "zone preview");
      node.style.borderStyle = "dashed";
      this._overlay.append(node);
    }
    node.style.left = box.x * 100 + "%";
    node.style.top = box.y * 100 + "%";
    node.style.width = box.w * 100 + "%";
    node.style.height = box.h * 100 + "%";
  }

  async _dragEnd() {
    const d = this._drag;
    this._drag = null;
    this._dragging = false;
    if (!d) return;
    const preview = this._overlay.querySelector(".zone.preview");
    if (preview) preview.remove();
    try {
      if (d.mode === "new") {
        const box = d.box;
        if (!box || box.w < 0.02 || box.h < 0.02) {
          this._paintZones();
          return;
        }
        await this._api("/zone", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "add", zone: { ...box, name: "Новая зона", role: "motion" } }),
        });
      } else if (d.mode === "vertex") {
        if (!d.points) {
          this._paintZones();
          return;
        }
        await this._api("/zone", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "save", id: d.id, zone: { points: d.points } }),
        });
      } else {
        if (!d.current) {
          this._paintZones();
          return;
        }
        await this._api("/zone", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "save", id: d.id, zone: d.current }),
        });
      }
      await this._tick();
    } catch (err) {
      this._status("не сохранено: " + err.message, true);
    }
    this._paintZones();
    this._paintPane();
  }

  // ------------------------------------------------------------------ панель
  _zone() {
    const zones = this._state?.settings?.zones || [];
    return zones.find((z) => z.id === this._selected) || zones[0] || null;
  }

  _select(id) {
    this._selected = id;
    this._paintZoneSelection();
    if (this._tab === "zones" || this._tab === "action") this._paintPane();
  }

  _paintPane() {
    if (!this._pane) return;
    if (this._tab === "schedules") this._tab = "action";
    this._pane.innerHTML = "";
    if (!this._state) {
      this._pane.append(h("div", "muted", "Загрузка…"));
      return;
    }
    if (!this._selected) this._selected = this._zone()?.id || null;
    if (this._tab === "zones") this._paneZones();
    else if (this._tab === "action") {
      this._paneAction();
      this._paneSchedules();
    } else if (this._tab === "detector") this._paneDetector();
    else if (this._tab === "interlocks") this._paneInterlocks();
    else if (this._tab === "source") this._paneSource();
    else if (this._tab === "help") this._paneHelp();
    else this._paneJournal();
    this._applyHints(this._pane);
  }

  _applyHints(root) {
    const MAP = {
      "Название": "Имя зоны: видно на кадре, в сущностях и журнале",
      "Роль": "«движение» — зона учитывается; «исключить» — маска помех",
      "Включена": "Выключенная зона показывает детекцию, но не запускает действия",
      "Чувствительность": "Множитель к порогу min_ratio: больше — чувствительнее",
      "Сущность": "Создать отдельный binary_sensor состояния этой зоны",
      "Форма": "Прямоугольник или произвольный многоугольник (тяните вершины на кадре)",
      "X, %": "Левый край зоны в процентах ширины кадра",
      "Y, %": "Верхний край зоны в процентах высоты кадра",
      "Ширина, %": "Ширина зоны в процентах кадра",
      "Высота, %": "Высота зоны в процентах кадра",
      "Устройство": "Что включать: свет, реле, розетка, сценарий или скрипт",
      "Держать, мин": "Сколько устройство горит после последнего движения",
      "Продлевать": "Новое движение во время удержания перезапускает таймер",
      "Максимум, мин": "Жёсткий лимит непрерывного горения, даже при постоянном движении",
      "Не гасить, если горело": "Если устройство горело до срабатывания — не выключать его по таймеру",
      "Пауза между вкл., мин": "Не включать повторно N минут после выключения (защита от мигания)",
      "Время суток": "Ограничение по солнцу: закат/восход (сенсор задаётся ниже)",
      "Время с": "Начало окна работы; пусто — круглосуточно",
      "Время по": "Конец окна работы; пусто — круглосуточно",
      "Дни": "Дни недели, в которые разрешены действия",
      "Сенсор «темно»": "sun.sun или свой датчик: состояние «on» = темно",
      "Поправка к закату, мин": "Минус — раньше (за 20 мин до заката), плюс — позже",
      "Не беспокоить": "Временно запретить действия (камера и журнал продолжают работать)",
      "Детектор": "Разность кадров (без зависимостей) или объекты (человек/машина)",
      "Сервис объектов": "HTTP-адрес ONNX-детектора (ARM:8099)",
      "Порог объектов": "Минимальная уверенность модели: ниже — больше находок и ложных",
      "Классы COCO": "0 человек · 1 велосипед · 2 машина · 3 мотоцикл · 5 автобус · 7 грузовик",
      "Только вместе с движением": "Объект засчитывается, только если в зоне есть изменение кадра",
      "Инференс только при движении": "Не тратить CPU на статичную сцену",
      "Дальние объекты": "Дробление кадра: выкл · если не нашли · всегда (медленно)",
      "Ширина кадра для детектора": "Для main-потока 1920 ставьте 960",
      "Мин. высота объекта, %": "Отсекать мелкие боксы; человек на дистанции пикапа ≈ 8 % кадра",
      "Ширина кадра, px": "До какого размера ужимать кадр для разности кадров",
      "Сглаживание": "0–3: гасит шум ИК и артефакты JPEG",
      "Порог разницы (день)": "Насколько пиксель должен измениться, чтобы считаться движением",
      "Порог разницы (ИК)": "То же для ч/б (ночь) — обычно ниже",
      "Мин. доля зоны, %": "Какая доля пикселей зоны должна измениться",
      "Мин. пикселей": "Абсолютный минимум изменённых пикселей (отсекает шум)",
      "Подтверждение, кадров": "Сколько кадров подряд нужно, чтобы признать движение",
      "Тишина, кадров": "Сколько тихих кадров подряд завершают движение",
      "Смена света, % кадра": "Если изменилось больше — это освещение, а не движение",
      "Ночной множитель": "Чувствительность в ИК-режиме (шум выше)",
      "Быстрое сглаживание фона": "Скорость адаптации фона: больше — быстрее привыкает",
      "Камера HA": "Приоритетный источник: сущность camera.*",
      "RTSP / URL / go2rtc": "Если камера не выбрана: rtsp://, http:// или имя потока go2rtc",
      "Интервал проверки, с": "Как часто опрашивать кадр (1–30 с)",
      "Название периода": "Видно в списке, журнале и датчиках",
      "Триггер периода": "По часам (19:00 → 07:00), с заката до рассвета или с рассвета до заката",
      "Начало периода": "С этого времени действия разрешены",
      "Конец периода": "После этого времени действия не выполняются; если раньше начала — переход через полночь",
      "Смещение начала, мин": "Сдвиг от заката/рассвета: −30 = за полчаса до",
      "Смещение конца, мин": "Сдвиг конца периода относительно рассвета/заката",
      "Держать при обнаружении, секунд": "0 — брать из зоны; иначе своё время удержания для этого периода",
      "Зона периода": "Для какой зоны действует период; «все зоны» — для всех",
      "Дни недели": "Дни, в которые период начинается",
      "Сущность ": "",
    };
    root.querySelectorAll(".row").forEach((row) => {
      const lbl = row.querySelector("label");
      if (!lbl) return;
      const key = lbl.textContent.trim();
      const hint = MAP[key];
      if (!hint) return;
      const nxt = row.nextElementSibling;
      if (nxt && nxt.classList && nxt.classList.contains("hint")) return;
      const node = document.createElement("div");
      node.className = "hint";
      node.textContent = hint;
      row.after(node);
    });
  }

  _row(labelText, control, hint) {
    const wrap = h("div");
    const row = h("div", "row");
    row.append(h("label", "", labelText), control);
    wrap.append(row);
    if (hint) wrap.append(h("div", "hint", hint));
    return wrap;
  }

  _number(value, onChange, opts = {}) {
    const input = document.createElement("input");
    input.type = "number";
    input.value = value;
    if (opts.min !== undefined) input.min = opts.min;
    if (opts.max !== undefined) input.max = opts.max;
    if (opts.step !== undefined) input.step = opts.step;
    input.onchange = () => onChange(input.value);
    return input;
  }

  _text(value, onChange) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    input.onchange = () => onChange(input.value);
    return input;
  }

  _checkbox(value, onChange) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!value;
    input.onchange = () => onChange(input.checked);
    return input;
  }

  _selectEl(options, value, onChange) {
    const sel = document.createElement("select");
    for (const [val, title] of options) {
      const opt = document.createElement("option");
      opt.value = val;
      opt.textContent = title;
      if (String(val) === String(value)) opt.selected = true;
      sel.append(opt);
    }
    sel.onchange = () => onChange(sel.value);
    return sel;
  }

  async _saveZone(zoneId, patch) {
    await this._api("/zone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "save", id: zoneId, zone: patch }),
    });
    await this._tick();
  }

  async _saveDetector(patch) {
    await this._api("/settings?refresh=1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ detector: patch }),
    });
    await this._tick();
  }

  _paneZones() {
    const list = h("div", "zlist");
    for (const zone of this._state.settings.zones) {
      const st = (this._state.zone_states || []).find((z) => z.id === zone.id) || {};
      const item = h("div", "zitem" + (zone.id === this._selected ? " on" : ""));
      item.onclick = () => this._select(zone.id);
      item.append(h("span", "name", zone.name));
      item.append(h("span", "mini", ROLE_LABEL[zone.role] || zone.role));
      item.append(h("span", "mini", st.active ? "движение" : pct(st.ratio)));
      const del = h("button", "act", "✕");
      del.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`Удалить зону «${zone.name}»?`)) return;
        await this._api("/zone", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete", id: zone.id }),
        });
        this._selected = null;
        await this._tick();
        this._paintPane();
      };
      item.append(del);
      list.append(item);
    }

    const zone = this._zone();
    this._pane.append(h("div", "muted", "Зоны: " + this._state.settings.zones.length), list);

    const add = h("button", "act primary", "＋ Добавить зону");
    add.style.marginTop = "8px";
    add.onclick = () => {
      this._pane.append(h("div", "muted", "Нарисуйте область мышью прямо на кадре"));
    };
    this._pane.append(add);

    if (zone) {
      this._pane.append(
        this._row("Название", this._text(zone.name, (v) => this._saveZone(zone.id, { name: v }))),
        this._row(
          "Роль",
          this._selectEl(
            [["motion", "движение"], ["ignore", "исключить"]],
            zone.role,
            (v) => this._saveZone(zone.id, { role: v })
          )
        ),
        this._row("Включена", this._checkbox(zone.enabled, (v) => this._saveZone(zone.id, { enabled: v }))),
        this._row(
          "Чувствительность",
          this._number(zone.sensitivity, (v) => this._saveZone(zone.id, { sensitivity: v }), { min: 0.2, max: 5, step: 0.1 })
        ),
        this._row("Сущность", this._checkbox(zone.entity, (v) => this._saveZone(zone.id, { entity: v }))),
        this._row("Форма", this._shapeButtons(zone)),
        this._row("X, %", this._number((zone.x * 100).toFixed(1), (v) => this._saveZone(zone.id, { x: Number(v) / 100 }), { min: 0, max: 100, step: 0.5 })),
        this._row("Y, %", this._number((zone.y * 100).toFixed(1), (v) => this._saveZone(zone.id, { y: Number(v) / 100 }), { min: 0, max: 100, step: 0.5 })),
        this._row("Ширина, %", this._number((zone.w * 100).toFixed(1), (v) => this._saveZone(zone.id, { w: Number(v) / 100 }), { min: 1, max: 100, step: 0.5 })),
        this._row("Высота, %", this._number((zone.h * 100).toFixed(1), (v) => this._saveZone(zone.id, { h: Number(v) / 100 }), { min: 1, max: 100, step: 0.5 }))
      );
    }
  }

  _entityOptions() {
    const opts = [["", "— не выбрано —"]];
    const states = this._hass ? this._hass.states : {};
    const wanted = ["switch.", "light.", "fan.", "input_boolean.", "scene.", "script."];
    const items = Object.keys(states)
      .filter((eid) => wanted.some((p) => eid.startsWith(p)))
      .map((eid) => {
        const name = states[eid].attributes?.friendly_name || eid;
        return [eid, `${name} · ${eid}`];
      })
      .sort((a, b) => a[1].localeCompare(b[1], "ru"));
    return opts.concat(items);
  }

  async _setMute(hours) {
    const until = hours > 0 ? new Date(Date.now() + hours * 3600 * 1000).toISOString() : "";
    await this._api("/settings?refresh=1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mute_until: until }),
    });
    await this._tick();
    this._paintPane();
  }

  async _saveGlobal(patch) {
    await this._api("/settings?refresh=1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    await this._tick();
    this._paintPane();
  }

  _shapeButtons(zone) {
    const isPoly = Array.isArray(zone.points) && zone.points.length >= 3;
    const wrap = h("div", "row");
    wrap.style.margin = "0";
    if (!isPoly) {
      const toPoly = h("button", "act", "⬡ В многоугольник");
      toPoly.onclick = () => {
        const pts = [
          [zone.x, zone.y],
          [zone.x + zone.w, zone.y],
          [zone.x + zone.w, zone.y + zone.h],
          [zone.x, zone.y + zone.h],
        ];
        this._saveZone(zone.id, { points: pts });
      };
      wrap.append(toPoly);
      wrap.append(h("span", "muted", "потом тяните вершины на кадре"));
    } else {
      const toRect = h("button", "act", "▭ В прямоугольник");
      toRect.onclick = () => this._saveZone(zone.id, { points: null });
      wrap.append(toRect);
      wrap.append(h("span", "muted", `${zone.points.length} вершин · правый клик по вершине удаляет · пунктирный квадрат добавляет`));
    }
    return wrap;
  }

  _paneAction() {
    const zone = this._zone();
    if (!zone) {
      this._pane.append(h("div", "muted", "Сначала создайте зону"));
      return;
    }
    const head = h("div", "", "Действия по движению");
    head.style.cssText = "margin:0 0 6px;font-weight:600;font-size:13px";
    this._pane.append(head);

    const info = (this._state.actions || {})[zone.target_entity] || null;
    const za = (this._state.zone_actions || {})[zone.id] || {};
    const fmtDur = (sec) => {
      const v = Math.max(0, Math.round(Number(sec) || 0));
      if (v < 60) return v + " с";
      const m = Math.floor(v / 60);
      const r = v % 60;
      return r ? `${m} мин ${r} с` : `${m} мин`;
    };
    const fmtClock = (iso) => {
      if (!iso) return "—";
      try {
        return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
      } catch (e) {
        return iso;
      }
    };

    // --- состояние действия
    const st = h("div");
    st.style.cssText = "border:1px solid var(--divider-color,#354354);border-radius:8px;padding:6px 8px;margin-bottom:8px";
    let line;
    if (za.cooldown_left > 0) {
      line = `пауза после выключения — ещё ${fmtDur(za.cooldown_left)}`;
    } else if (za.active) {
      line = `включено · осталось ${fmtDur(za.left_sec)} (до лимита ${fmtDur(za.max_left_sec)})`;
    } else {
      line = "ожидание движения";
    }
    st.append(
      h("div", "muted", "Состояние действия"),
      h("div", "", line),
      h("div", "muted", `устройство: ${za.entity_state || "—"}${za.entity_changed ? " (изменено " + fmtClock(za.entity_changed) + ")" : ""}${za.last_command_sec !== null && za.last_command_sec !== undefined ? " · команда " + fmtDur(za.last_command_sec) + " назад" : ""}`),
      h("div", "muted", `держать ${(zone.hold_sec / 60).toFixed(0)} мин · лимит ${(zone.max_on_sec / 60).toFixed(0)} мин · пауза ${((zone.cooldown_sec || 0) / 60).toFixed(0)} мин${zone.restore ? " · не гасить, если горело" : ""}`)
    );
    const quick = h("div", "row");
    quick.style.margin = "6px 0 0";
    const bOff = h("button", "act", "Выключить сейчас");
    bOff.onclick = () => this._testAction("off", zone.id);
    const bCool = h("button", "act", "Сбросить паузу");
    bCool.onclick = async () => {
      try {
        await this._api("/test?entry_id=" + (this._entry || ""), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "reset_cooldown", zone_id: zone.id }),
        });
        await this._tick();
        this._paintPane();
      } catch (err) {
        this._status("пауза не сброшена: " + err.message, true);
      }
    };
    quick.append(bOff, bCool);
    st.append(quick);
    this._pane.append(st);

    // --- расписание/солнце
    const sunEntity = this._state.settings.sun_entity || "sun.sun";
    const sunState = this._hass.states[sunEntity];
    const sunAttrs = sunState?.attributes || {};
    const isNight = sunEntity === "sun.sun" ? sunState?.state === "below_horizon" : String(sunState?.state || "").toLowerCase() === "on";
    const winFrom = zone.time_from || "00:00";
    const winTo = zone.time_to || "24:00";
    const dayNames = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
    const daysText = (zone.days || []).length === 7 ? "каждый день" : (zone.days || []).map((d) => dayNames[d]).join(", ") || "—";
    const sch = h("div");
    sch.style.cssText = "border:1px solid var(--divider-color,#354354);border-radius:8px;padding:6px 8px;margin-bottom:8px";
    sch.append(
      h("div", "muted", "Расписание"),
      h("div", "", `сейчас: ${isNight ? "ночь" : "день"} · закат ${fmtClock(sunAttrs.next_setting)} · восход ${fmtClock(sunAttrs.next_rising)}`),
      h("div", "muted", `окно ${winFrom}–${winTo} · ${daysText} · условие: ${zone.sun === "night" ? "только после заката" : zone.sun === "day" ? "только до заката" : "всегда"}`),
      h("div", "muted", `поправка к закату: ${this._state.settings.sun_offset_min || 0} мин · сенсор: ${sunEntity}`)
    );
    this._pane.append(sch);

    const muteRow = h("div", "row");
    muteRow.append(h("label", "", "Не беспокоить"));
    for (const hrs of [1, 4, 8]) {
      const b = h("button", "act", hrs + " ч");
      b.onclick = () => this._setMute(hrs);
      muteRow.append(b);
    }
    const muteOff = h("button", "act", "Снять");
    muteOff.onclick = () => this._setMute(0);
    muteRow.append(muteOff);
    this._pane.append(muteRow);
    const muted = this._state.settings.mute_until ? new Date(this._state.settings.mute_until) : null;
    if (muted && muted > new Date()) {
      this._pane.append(h("div", "muted", "Действует до " + muted.toLocaleString("ru-RU")));
    }

    const sunOptions = [["sun.sun", "sun.sun (встроенный, закат/восход)"]];
    for (const eid of Object.keys(this._hass.states)) {
      if (eid.startsWith("binary_sensor.") && /dark|night|sun|light|темен|ночь|освещ/i.test(eid)) {
        sunOptions.push([eid, (this._hass.states[eid].attributes?.friendly_name || eid) + " · " + eid]);
      }
    }
    this._pane.append(
      this._row(
        "Сенсор «темно»",
        this._selectEl(sunOptions, this._state.settings.sun_entity || "sun.sun", (v) => this._saveGlobal({ sun_entity: v }))
      ),
      this._row(
        "Поправка к закату, мин",
        this._number(this._state.settings.sun_offset_min || 0, (v) => this._saveGlobal({ sun_offset_min: Number(v) }), { min: -180, max: 180, step: 5 })
      )
    );

    this._pane.append(h("div", "muted", "Зона: " + zone.name));

    this._pane.append(
      this._row(
        "Устройство",
        this._selectEl(this._entityOptions(), zone.target_entity, (v) => this._saveZone(zone.id, { target_entity: v }))
      ),
      this._row(
        "Держать, мин",
        this._number((zone.hold_sec / 60).toFixed(1), (v) => this._saveZone(zone.id, { hold_sec: Math.round(Number(v) * 60) }), {
          min: 0.1,
          max: 1440,
          step: 0.5,
        })
      ),
      this._row("Продлевать", this._checkbox(zone.extend, (v) => this._saveZone(zone.id, { extend: v }))),
      this._row(
        "Максимум, мин",
        this._number((zone.max_on_sec / 60).toFixed(0), (v) => this._saveZone(zone.id, { max_on_sec: Math.round(Number(v) * 60) }), {
          min: 1,
          max: 1440,
          step: 1,
        })
      ),
      this._row("Не гасить, если горело", this._checkbox(zone.restore, (v) => this._saveZone(zone.id, { restore: v }))),
      this._row(
        "Пауза между вкл., мин",
        this._number(((zone.cooldown_sec || 0) / 60).toFixed(1), (v) => this._saveZone(zone.id, { cooldown_sec: Math.round(Number(v) * 60) }), { min: 0, max: 1440, step: 0.5 })
      ),
      this._row(
        "Время суток",
        this._selectEl(
          [["any", "всегда"], ["night", "только после заката"], ["day", "только до заката"]],
          zone.sun || "any",
          (v) => this._saveZone(zone.id, { sun: v })
        )
      ),
      this._row("Время с", this._text(zone.time_from, (v) => this._saveZone(zone.id, { time_from: v }))),
      this._row("Время по", this._text(zone.time_to, (v) => this._saveZone(zone.id, { time_to: v })))
    );

    const daysRow = h("div", "row");
    daysRow.append(h("label", "", "Дни"));
    const days = h("div", "days");
    for (let i = 0; i < 7; i++) {
      const lbl = h("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = (zone.days || []).includes(i);
      cb.onchange = () => {
        const set = new Set(zone.days || []);
        if (cb.checked) set.add(i);
        else set.delete(i);
        this._saveZone(zone.id, { days: Array.from(set).sort() });
      };
      lbl.append(cb, document.createTextNode(DAYS[i]));
      days.append(lbl);
    }
    daysRow.append(days);
    this._pane.append(daysRow);

    if (info) {
      this._pane.append(
        h("div", "muted", `Сейчас включено этим правилом: осталось ${Math.round(info.left_sec / 60)} мин (лимит ${Math.round(info.max_left_sec / 60)} мин)`)
      );
    }

    const testRow = h("div", "row");
    const on = h("button", "act primary", "Включить на 10 с");
    on.onclick = () => this._testAction("on", zone.id);
    const off = h("button", "act", "Выключить");
    off.onclick = () => this._testAction("off", zone.id);
    testRow.append(on, off);
    this._pane.append(testRow);
  }

  _ruleHit(rule) {
    const st = this._hass.states[rule.entity];
    if (!st) return [false, "нет сущности"];
    const op = rule.op || "is_on";
    const raw = rule.attribute ? st.attributes?.[rule.attribute] : st.state;
    const stt = String(st.state).toLowerCase();
    const on = ["on", "true", "1", "yes", "home", "open", "opened"].includes(stt);
    const off = ["off", "false", "0", "no", "not_home", "closed"].includes(stt);
    if (op === "is_on") return [on, stt];
    if (op === "is_off") return [off, stt];
    const n = Number(raw);
    const v = Number(rule.value);
    if (!Number.isFinite(n) || !Number.isFinite(v)) return [false, "—"];
    const hit =
      op === "above" ? n > v : op === "below" ? n < v : op === "equal" ? Math.abs(n - v) < 1e-9 : Math.abs(n - v) > 1e-9;
    return [hit, String(n)];
  }

  async _saveInterlocks(rules) {
    await this._api("/settings?refresh=1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ interlocks: rules }),
    });
    await this._tick();
    this._paintPane();
  }

  _paneInterlocks() {
    const rules = (this._state.settings.interlocks || []).slice();
    const OPS = [
      ["is_on", "включён / открыт"],
      ["is_off", "выключен / закрыт"],
      ["above", "больше"],
      ["below", "меньше"],
      ["equal", "равно"],
      ["not_equal", "не равно"],
    ];
    const MODES = [["block", "запрещать"], ["warn", "предупреждать"]];

    this._pane.append(
      h("div", "muted", "Пока условие выполняется, действие не выполняется (режим «запрещать») либо только пишется в журнал.")
    );

    if (!rules.length) this._pane.append(h("div", "muted", "Правил пока нет"));
    rules.forEach((rule, idx) => {
      const [hit, value] = this._ruleHit(rule);
      const card = h("div", "zitem" + (hit && rule.mode === "block" ? " on" : ""));
      card.style.flexDirection = "column";
      card.style.alignItems = "stretch";
      const head = h("div", "row");
      head.style.margin = "0";
      const en = document.createElement("input");
      en.type = "checkbox";
      en.checked = rule.enabled !== false;
      en.onchange = () => {
        const next = rules.slice();
        next[idx] = { ...rule, enabled: en.checked };
        this._saveInterlocks(next);
      };
      const nameInput = this._text(rule.name, (v) => {
        const next = rules.slice();
        next[idx] = { ...rule, name: v };
        this._saveInterlocks(next);
      });
      nameInput.style.flex = "1 1 auto";
      const del = h("button", "act", "✕");
      del.onclick = () => this._saveInterlocks(rules.filter((r) => r.id !== rule.id));
      head.append(en, nameInput, del);
      card.append(head);

      const entInput = this._text(rule.entity, (v) => {
        const next = rules.slice();
        next[idx] = { ...rule, entity: v };
        this._saveInterlocks(next);
      });
      entInput.setAttribute("list", "lbv-entities");
      const opSel = this._selectEl(OPS, rule.op, (v) => {
        const next = rules.slice();
        next[idx] = { ...rule, op: v };
        this._saveInterlocks(next);
      });
      const modeSel = this._selectEl(MODES, rule.mode, (v) => {
        const next = rules.slice();
        next[idx] = { ...rule, mode: v };
        this._saveInterlocks(next);
      });
      const valInput = this._number(rule.value ?? "", (v) => {
        const next = rules.slice();
        next[idx] = { ...rule, value: v === "" ? null : Number(v) };
        this._saveInterlocks(next);
      }, { step: 1 });
      card.append(this._row("Сущность", entInput), this._row("Условие", opSel), this._row("Значение", valInput), this._row("Режим", modeSel));
      card.append(h("div", "muted", `сейчас: ${hit ? "срабатывает" : "нет"} (${value})`));
      this._pane.append(card);
    });

    const dl = document.createElement("datalist");
    dl.id = "lbv-entities";
    for (const eid of Object.keys(this._hass.states)) {
      const o = document.createElement("option");
      o.value = eid;
      dl.append(o);
    }
    this._pane.append(dl);

    const add = h("button", "act primary", "＋ Добавить запрет");
    add.style.marginTop = "8px";
    add.onclick = () => {
      const next = rules.concat([
        { id: "i" + Date.now(), name: "Новый запрет", entity: "", op: "is_on", value: null, mode: "block", enabled: true },
      ]);
      this._saveInterlocks(next);
    };
    this._pane.append(add);
    this._pane.append(
      h("div", "muted", "Примеры: «Ворота открыты» (switch/вход is_on → запрещать), «Свет уже горит» (устройство is_on), «На улице светло» (датчик освещённости above 100).")
    );
  }

  async _saveSchedules(list) {
    await this._api("/settings?refresh=1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ schedules: list }),
    });
    await this._tick();
    this._paintPane();
  }

  _schedTitle(trigger) {
    return {
      time: "по времени",
      sunset: "с заката до рассвета",
      sunrise: "с рассвета до заката",
    }[trigger] || "по времени";
  }

  _schedSummary(sched, item, dayNames) {
    const trig = this._schedTitle(sched.trigger || "time");
    const win = item && item.start ? `${fmtClock(item.start)} → ${fmtClock(item.end)}` : "—";
    const hold = Number(sched.seconds || 0) > 0
      ? `держать ${Math.round(sched.seconds / 60)} мин`
      : "держать как в зоне";
    const days = (sched.days || []).length === 7
      ? "каждый день"
      : (sched.days || []).map((d) => dayNames[d]).join(", ");
    const state = item ? (item.active ? " · сейчас активен" : " · вне периода") : "";
    return `${trig}: ${win} · ${hold} · ${days}${state}`;
  }

  _paneSchedules() {
    const head = h("div", "", "Периоды работы (когда действия разрешены)");
    head.style.cssText = "margin:16px 0 4px;padding-top:10px;border-top:1px solid var(--divider-color,#354354);font-weight:600;font-size:13px";
    this._pane.append(head);

    const list = (this._state.settings.schedules || []).slice();
    const zones = this._state.settings.zones || [];
    const zoneOptions = [["", "все зоны"], ...zones.map((z) => [z.id, z.name])];
    const zoneName = (id) => (zones.find((z) => z.id === id) || {}).name || "все зоны";
    const dayNames = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
    const window = this._state.window || null;
    const items = (window && window.items) || [];
    const byId = {};
    items.forEach((it) => { byId[it.id] = it; });
    const nxt = (this._state.next_schedule) || null;
    const activeNames = items.filter((i) => i.active).map((i) => i.name);

    this._pane.append(
      h("div", "muted", "Период работы: внутри периода обнаружение человека включает устройство на «держать» (из зоны) и затем выключает. Вне периода действия не выполняются. Период может быть по времени (например 19:00 → 07:00), с заката до рассвета или с рассвета до заката.")
    );
    if (activeNames.length) {
      this._pane.append(h("div", "muted", `Сейчас период активен: ${activeNames.join(", ")}${window.until ? " — до " + fmtClock(window.until) : ""}`));
    } else if (items.length) {
      this._pane.append(h("div", "muted", "Сейчас вне периода работы — действия не выполняются."));
    }
    if (nxt) {
      this._pane.append(
        h("div", "muted", `Ближайший период: ${nxt.name || "—"} — начнётся ${fmt(nxt.at)} (${this._schedTitle(nxt.trigger)})`)
      );
    }

    if (!list.length) {
      this._pane.append(h("div", "muted", "Периодов пока нет — действия разрешены круглосуточно."));
    }

    list.forEach((sched, idx) => {
      const card = h("div", "zitem");
      card.style.flexDirection = "column";
      card.style.alignItems = "stretch";
      const head = h("div", "row");
      head.style.margin = "0";
      const en = document.createElement("input");
      en.type = "checkbox";
      en.checked = sched.enabled !== false;
      en.onchange = () => {
        const next = list.slice();
        next[idx] = { ...sched, enabled: en.checked };
        this._saveSchedules(next);
      };
      const name = this._text(sched.name, (v) => {
        const next = list.slice();
        next[idx] = { ...sched, name: v };
        this._saveSchedules(next);
      });
      name.style.flex = "1 1 auto";
      const del = h("button", "act", "✕");
      del.onclick = () => this._saveSchedules(list.filter((x) => x.id !== sched.id));
      head.append(en, name, del);
      card.append(head);
      card.append(h("div", "muted", this._schedSummary(sched, byId[sched.id], dayNames)));
      card.append(h("div", "muted", `зона: ${zoneName(sched.zone_id)} · ${sched.trigger === "time" ? "по часам" : "по солнцу"}`));
      this._pane.append(card);
    });

    // ------- форма добавления (черновик сохраняется между перерисовками)
    const draft = this._schedDraft || (this._schedDraft = {
      name: "Ночной период",
      trigger: "time",
      time_from: "19:00",
      time_to: "07:00",
      offset_min: 0,
      offset_end_min: 0,
      seconds: 0,
      days: [0, 1, 2, 3, 4, 5, 6],
      zone_id: "",
    });

    const nameInput = this._text(draft.name, (v) => { draft.name = v; });
    const triggerSel = this._selectEl(
      [["time", "по времени"], ["sunset", "с заката до рассвета"], ["sunrise", "с рассвета до заката"]],
      draft.trigger,
      (v) => { draft.trigger = v; this._paintPane(); }
    );
    const fromInput = document.createElement("input");
    fromInput.type = "time";
    fromInput.value = draft.time_from;
    fromInput.onchange = () => { draft.time_from = fromInput.value; };
    const toInput = document.createElement("input");
    toInput.type = "time";
    toInput.value = draft.time_to;
    toInput.onchange = () => { draft.time_to = toInput.value; };
    const offIn = this._number(draft.offset_min, (v) => { draft.offset_min = Number(v); }, { min: -300, max: 300, step: 5 });
    const offEndIn = this._number(draft.offset_end_min, (v) => { draft.offset_end_min = Number(v); }, { min: -300, max: 300, step: 5 });
    const secsInput = this._number(draft.seconds, (v) => { draft.seconds = Number(v); }, { min: 0, max: 86400, step: 30 });
    const zoneSelEl = this._selectEl(zoneOptions, draft.zone_id, (v) => { draft.zone_id = v; });

    const daysRow = h("div", "row");
    daysRow.append(h("label", "", "Дни недели"));
    const daysWrap = h("div", "days");
    for (let i = 0; i < 7; i++) {
      const lbl = h("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = (draft.days || []).includes(i);
      cb.onchange = () => {
        if (cb.checked) draft.days = Array.from(new Set([...(draft.days || []), i])).sort();
        else draft.days = (draft.days || []).filter((d) => d !== i);
      };
      lbl.append(cb, document.createTextNode(dayNames[i]));
      daysWrap.append(lbl);
    }
    daysRow.append(daysWrap);

    const rows = [this._row("Название периода", nameInput, "Видно в списке, журнале и датчиках")];
    rows.push(this._row("Триггер периода", triggerSel, "Чем определяются начало и конец периода"));
    if (draft.trigger === "time") {
      rows.push(this._row("Начало периода", fromInput, "С этого времени действия разрешены"));
      rows.push(this._row("Конец периода", toInput, "После этого времени действия не выполняются; если раньше начала — значит переход через полночь"));
    } else {
      rows.push(this._row("Смещение начала, мин", offIn, "Сдвиг от заката/рассвета: −30 = за полчаса до"));
      rows.push(this._row("Смещение конца, мин", offEndIn, "Сдвиг конца периода относительно рассвета/заката"));
    }
    rows.push(this._row("Держать при обнаружении, секунд", secsInput, "0 — брать из зоны; иначе своё время для этого периода"));
    rows.push(this._row("Зона периода", zoneSelEl, "Для какой зоны действует период; «все зоны» — для всех"));
    rows.push(daysRow);

    this._pane.append(h("div", "muted", "Новый период"), ...rows);

    const add = h("button", "act primary", "＋ Добавить период");
    add.onclick = () => {
      const item = {
        id: "s" + Date.now(),
        name: draft.name || "Период",
        enabled: true,
        trigger: draft.trigger,
        time_from: draft.time_from,
        time_to: draft.time_to,
        offset_min: draft.offset_min,
        offset_end_min: draft.offset_end_min,
        days: (draft.days || []).length ? draft.days : [0, 1, 2, 3, 4, 5, 6],
        zone_id: draft.zone_id,
        seconds: draft.seconds,
        last_start: "",
        last_end: "",
      };
      this._schedDraft = null;
      this._saveSchedules(list.concat([item]));
    };
    this._pane.append(add);
    this._pane.append(
      h("div", "muted", "Если периодов нет — ограничений нет (действия круглосуточно). Проверка периода — каждые 20 секунд; в конце периода устройство выключается принудительно.")
    );
  }

  _paneSource() {
    const st = this._state.settings;
    const camOptions = [["", "— не выбрана (RTSP / URL) —"]];
    for (const eid of Object.keys(this._hass.states)) {
      if (eid.startsWith("camera.")) {
        const name = this._hass.states[eid].attributes?.friendly_name || eid;
        camOptions.push([eid, `${name} · ${eid}`]);
      }
    }
    let camera = st.camera_entity || "";
    let url = st.snapshot_url || "";
    let interval = st.scan_interval;

    const apply = h("button", "act primary", "Применить источник");
    apply.style.marginTop = "10px";
    apply.onclick = async () => {
      try {
        await this._api("/settings?refresh=1", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ camera_entity: camera, snapshot_url: url, scan_interval: Number(interval) }),
        });
        this._zonesSig = null;
        await this._tick();
        this._status("источник применён");
      } catch (err) {
        this._status("источник не применён: " + err.message, true);
      }
    };

    this._pane.append(
      h("div", "muted", "Камера Home Assistant имеет приоритет. Иначе — поток RTSP, URL снапшота или имя потока go2rtc."),
      this._row("Камера HA", this._selectEl(camOptions, st.camera_entity || "", (v) => { camera = v; })),
      this._row("RTSP / URL / go2rtc", this._text(st.snapshot_url, (v) => { url = v; })),
      this._row("Интервал проверки, с", this._number(st.scan_interval, (v) => { interval = v; }, { min: 1, max: 30, step: 0.5 })),
      apply,
      h("div", "muted", "Сейчас: " + (st.camera_entity || st.snapshot_url || "не задано"))
    );
  }

  async _applyPreset(name) {
    const presets = {
      street: { mode: "objects", objects_conf: 0.25, objects_tiling: 2, objects_require_motion: true, objects_only_on_motion: true, min_ratio: 0.012, diff_t: 24 },
      yard: { mode: "objects", objects_conf: 0.3, objects_tiling: 1, objects_require_motion: true, objects_only_on_motion: true, min_ratio: 0.015, diff_t: 26 },
      room: { mode: "frame", objects_tiling: 1, min_ratio: 0.02, diff_t: 28, blur: 1 },
    };
    await this._saveDetector(presets[name] || {});
  }

  _calibrationBlock(d) {
    const cal = this._state.settings.calibration || {};
    const wrap = h("div");
    wrap.style.cssText = "border:1px solid var(--divider-color,#354354);border-radius:8px;padding:6px 8px;margin-bottom:8px";
    const ppx = Number(cal.px_per_m || 0) * Number(cal.person_m || 1.7);
    const modelPx = ppx * 640 / Number(cal.frame_w || 1920);
    const minH = Math.max(0.005, Math.min(0.05, (ppx / Number(cal.frame_h || 1080)) * 0.25));
    wrap.append(
      h("div", "muted", "Калибровка масштаба"),
      h("div", "muted", cal.object_name ? `${cal.object_name}: ${cal.object_px} px / ${cal.object_m} м` : "не заполнена"),
      h("div", "muted", `номер: ${cal.plate_px || 0} px / ${cal.plate_m || 0.52} м · масштаб ${cal.px_per_m || 0} px/м`),
      h("div", "muted", `человек ${cal.person_m || 1.7} м → ${ppx.toFixed(0)} px в кадре, ${modelPx.toFixed(0)} px в модели; рекомендация: tiling ${modelPx >= 40 ? 1 : 2}, min_h ${minH.toFixed(3)}`)
    );
    const apply = h("button", "act primary", "Применить по калибровке");
    apply.style.marginTop = "6px";
    apply.onclick = async () => {
      try {
        await this._api("/settings?refresh=1", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ apply_calibration: true }),
        });
        await this._tick();
        this._paintPane();
        this._status("применено по калибровке");
      } catch (err) {
        this._status("не применено: " + err.message, true);
      }
    };
    wrap.append(apply);
    return wrap;
  }

  _paneDetector() {
    const d = this._state.settings.detector;
    const obj = this._state.objects || {};
    const modeSelect = this._selectEl(
      [["frame", "разность кадров (без зависимостей)"], ["objects", "объекты: человек/машина (ONNX)"]],
      d.mode || "frame",
      (v) => this._saveDetector({ mode: v })
    );
    const presetRow = h("div", "row");
    presetRow.append(h("label", "", "Пресеты"));
    for (const [key, title] of [["street", "Улица (дальние)"], ["yard", "Двор"], ["room", "Помещение"]]) {
      const b = h("button", "act", title);
      b.onclick = () => this._applyPreset(key);
      presetRow.append(b);
    }
    this._pane.append(presetRow);

    this._pane.append(this._calibrationBlock(d));

    this._pane.append(
      this._row("Детектор", modeSelect),
      this._row("Сервис объектов", this._text(d.objects_url, (v) => this._saveDetector({ objects_url: v }))),
      this._row("Порог объектов", this._number(d.objects_conf, (v) => this._saveDetector({ objects_conf: v }), { min: 0.05, max: 0.99, step: 0.05 })),
      this._row("Классы COCO", this._text(d.objects_classes, (v) => this._saveDetector({ objects_classes: v }))),
      this._row("Только вместе с движением", this._checkbox(d.objects_require_motion !== false, (v) => this._saveDetector({ objects_require_motion: v }))),
      this._row("Инференс только при движении", this._checkbox(d.objects_only_on_motion !== false, (v) => this._saveDetector({ objects_only_on_motion: v }))),
      this._row(
        "Дальние объекты",
        this._selectEl(
          [[1, "выкл"], [2, "дробить, если не нашли"], [3, "дробить всегда (медленно)"]],
          Number(d.objects_tiling || 1),
          (v) => this._saveDetector({ objects_tiling: Number(v) })
        )
      ),
      this._row("Ширина кадра для детектора", this._number(d.objects_width || 0, (v) => this._saveDetector({ objects_width: Number(v) }), { min: 0, max: 1920, step: 40 })),
      this._row("Мин. высота объекта, %", this._number(((d.objects_min_h || 0) * 100).toFixed(1), (v) => this._saveDetector({ objects_min_h: Number(v) / 100 }), { min: 0, max: 50, step: 0.5 })),
      h("div", "muted", `Детектор: ${(d.mode || "frame") === "objects" ? (obj.ok === false ? "ошибка — " + (obj.error || "") : `ок, объектов ${obj.count ?? 0}, ${Math.round(obj.ms || 0)} мс`) : "разность кадров"}`)
    );
    this._pane.append(
      this._row("Ширина кадра, px", this._number(d.width, (v) => this._saveDetector({ width: v }), { min: 160, max: 1280, step: 20 })),
      this._row("Сглаживание", this._number(d.blur, (v) => this._saveDetector({ blur: v }), { min: 0, max: 3, step: 1 })),
      this._row("Порог разницы (день)", this._number(d.diff_t, (v) => this._saveDetector({ diff_t: v }), { min: 3, max: 120, step: 1 })),
      this._row("Порог разницы (ИК)", this._number(d.diff_t_ir, (v) => this._saveDetector({ diff_t_ir: v }), { min: 2, max: 120, step: 1 })),
      this._row("Мин. доля зоны, %", this._number((d.min_ratio * 100).toFixed(2), (v) => this._saveDetector({ min_ratio: Number(v) / 100 }), { min: 0.05, max: 50, step: 0.05 })),
      this._row("Мин. пикселей", this._number(d.min_px, (v) => this._saveDetector({ min_px: v }), { min: 5, max: 5000, step: 5 })),
      this._row("Подтверждение, кадров", this._number(d.confirm_frames, (v) => this._saveDetector({ confirm_frames: v }), { min: 1, max: 10, step: 1 })),
      this._row("Тишина, кадров", this._number(d.quiet_frames, (v) => this._saveDetector({ quiet_frames: v }), { min: 1, max: 60, step: 1 })),
      this._row("Смена света, % кадра", this._number((d.global_change_ratio * 100).toFixed(0), (v) => this._saveDetector({ global_change_ratio: Number(v) / 100 }), { min: 10, max: 100, step: 5 })),
      this._row("Ночной множитель", this._number(d.night_sensitivity, (v) => this._saveDetector({ night_sensitivity: v }), { min: 0.5, max: 5, step: 0.1 })),
      this._row("Быстрое сглаживание фона", this._number(d.bg_alpha, (v) => this._saveDetector({ bg_alpha: v }), { min: 0.005, max: 0.5, step: 0.005 })),
      h("div", "muted", "Пороги применяются сразу. Нажмите «Показать маску», чтобы увидеть, что считается движением.")
    );
  }

  _paneHelp() {
    const section = (title, items) => {
      const box = h("div");
      box.append(h("h3", "", title));
      const ul = h("ul");
      for (const item of items) {
        const li = h("li");
        if (typeof item === "string") li.textContent = item;
        else li.append(item);
        ul.append(li);
      }
      box.append(ul);
      return box;
    };
    const code = (text) => {
      const el = document.createElement("code");
      el.textContent = text;
      return el;
    };

    const wrap = h("div", "help");
    wrap.append(
      section("Что делает интеграция", [
        "Следит за кадром камеры и включает выбранное устройство (свет, реле, прожектор), когда в нарисованной области появляется движение.",
        "Держит устройство заданное время после последнего движения, продлевает при новом движении и выключает по тишине или лимиту.",
        "Не трогает устройство, если оно уже горело до срабатывания (настройка «Не гасить, если горело»).",
      ]),
      section("Зоны", [
        ["Область на кадре — прямоугольник или произвольный многоугольник (кнопка «⬡ В многоугольник»). Вершины тянутся мышью, пунктирный маркер на ребре добавляет вершину, правый клик удаляет."],
        ["Роль «исключить» — маска помех: дорога, ствол дерева, качающаяся ветка."],
        ["Чувствительность — множитель к общему порогу: 2.0 сработает на вдвое меньшее изменение."],
        "Точные X/Y/Ш/В — положение и размер в процентах кадра (для точной подгонки).",
      ]),
      section("Действия", [
        "Держать — сколько устройство остаётся включённым после последнего движения.",
        "Максимум — жёсткий лимит непрерывного горения (даже при постоянном движении).",
        "Пауза между включениями — защита от частого мигания (после выключения N минут не включать).",
        "Продлевать — новое движение во время удержания сбрасывает таймер.",
        "Время суток — ограничение по сенсору «темно» (закат/восход) с поправкой в минутах.",
        "После каждой команды интеграция через 5 секунд проверяет фактическое состояние устройства, при расхождении повторяет (до 3 раз).",
        ["«Не беспокоить» и кнопка «Выключить сейчас» — ручное управление; пауза снимается кнопкой «Сбросить паузу»."],
      ]),
      section("Детектор", [
        "Режим «разность кадров» — без зависимостей: фон подстраивается, сравниваются пиксели, смена освещения отсекается по доле изменённого кадра.",
        "Режим «объекты» — кадр отправляется в ONNX-детектор (ARM:8099); в зоне засчитываются только человек/машина и т.п.",
        "«Только вместе с движением» — объект подтверждается лишь при изменении кадра (припаркованная машина не держит свет).",
        "«Инференс только при движении» — модель не вызывается на статичной сцене (экономия CPU).",
        "«Дальние объекты» — дробление кадра на фрагменты: лучше видно мелкие/дальние фигуры, но медленнее.",
        "Калибровка масштаба: эталонный объект и номер (0,52 м) дают px/м; кнопка «Применить по калибровке» выставляет tiling/min_h/conf.",
        ["Формулы: ", code("px_per_m = ширина_px / ширина_м"), " · ", code("person_px = 1.70 × px_per_m"), " · ", code("в модели = person_px × 640 / ширина_кадра × tiles")],
      ]),
      section("Запреты", [
        "Правило = сущность + условие (включён/выключен/больше/меньше/равно/не равно) + значение + режим.",
        "«Запрещать» — действие не выполняется (причина в журнале), «Предупреждать» — только запись в лог.",
        "Примеры: ворота открыты, свет уже горит, на улице светло.",
      ]),
      section("Периоды работы (расписания)", [
        "Расписание — это ПЕРИОД, в котором разрешены действия. Внутри периода обнаружение человека включает устройство на «держать» и затем выключает; вне периода действия не выполняются.",
        "Типы периода: «по времени» (например 19:00 → 07:00, переход через полночь поддержан), «с заката до рассвета», «с рассвета до заката». Закат и рассвет считает Home Assistant по координатам, смещения задаются в минутах.",
        "Дни недели относятся к дню начала периода. Если периодов нет — ограничений нет (круглосуточно).",
        "В конце периода интеграция принудительно выключает то, что включила сама.",
        "Проверка каждые 20 секунд; срабатывание проходит те же проверки (запреты, «не беспокоить», пауза).",
        "Догон пропущенного: если HA был выключен, событие выполнится, пока опоздание меньше «догон, мин» (по умолчанию 360), иначе пометится пропущенным.",
        "Кнопка «Запустить» и сервис run_schedule позволяют выполнить правило вручную.",
      ]),
      section("Источник кадра", [
        "Можно выбрать камеру Home Assistant (приоритет) или указать RTSP/URL/имя потока go2rtc.",
        "Интервал проверки — как часто опрашивать кадр. Для света достаточно 1,5–2 с.",
        "Состояние источника видно в binary_sensor «Источник кадра» (атрибут error).",
      ]),
      section("Сущности и сервисы", [
        ["Движение по зоне — ", code("binary_sensor.<камера>_zona_N")],
        ["Статус, последнее действие, ближайшее выключение/расписание — ", code("sensor.<камера>_status / _last_action / _next_event / _next_schedule")],
        ["Сервисы: ", code("light_by_video.trigger"), ", ", code("light_by_video.stop"), ", ", code("light_by_video.reset_cooldown"), ", ", code("light_by_video.run_schedule")],
        ["HTTP API: ", code("/api/light_by_video/state|frame|mask|settings|zone|test|events")],
      ]),
      section("Подсказки по настройке", [
        "Начните с «Показать маску» — видно, что считается движением.",
        "Много ложных — поднимите «Мин. доля зоны» и «Порог разницы», добавьте зоны-исключения.",
        "Пропускает человека — снизьте порог, включите «Дальние объекты» = «дробить, если не нашли».",
        "Ночью шумно — поднимите «Порог разницы (ИК)» и «Ночной множитель».",
      ])
    );
    this._pane.append(wrap);
  }

  _paneJournal() {
    const wrap = h("div");
    wrap.id = "journal";
    this._pane.append(wrap);
    this._paintJournal();
  }

  _paintJournal() {
    const wrap = this._pane?.querySelector("#journal");
    if (!wrap || !this._state) return;
    wrap.innerHTML = "";
    const events = (this._state.event_log || []).slice().reverse();
    if (!events.length) {
      wrap.append(h("div", "muted", "Пока пусто"));
      return;
    }
    const table = document.createElement("table");
    const head = document.createElement("tr");
    ["время", "событие", "детали"].forEach((t) => head.append(h("th", "", t)));
    table.append(head);
    for (const e of events.slice(0, 60)) {
      const tr = document.createElement("tr");
      tr.append(h("td", "", fmt(e.ts)));
      tr.append(h("td", "", e.title || e.event));
      const details = [e.zone, e.reason, e.entity].filter(Boolean).join(" · ");
      tr.append(h("td", "", details));
      table.append(tr);
    }
    wrap.append(table);
  }

  async _test() {
    try {
      const res = await this._api("/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      this._state = { ...(this._state || {}), ...(res.state || {}) };
      this._paintStatus();
    } catch (err) {
      this._status("проверка не удалась: " + err.message, true);
    }
  }

  async _testAction(action, zoneId) {
    try {
      await this._api("/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, zone_id: zoneId }),
      });
      await this._tick();
    } catch (err) {
      this._status("действие не выполнено: " + err.message, true);
    }
  }

  _status(text, bad) {
    if (!this._statusEl) return;
    this._statusEl.textContent = text;
    this._statusEl.style.color = bad ? "#f85149" : "";
  }
}

if (!customElements.get("light-by-video-panel")) customElements.define("light-by-video-panel", LightByVideoPanel);
