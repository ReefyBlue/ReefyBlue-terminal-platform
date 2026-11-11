// Terminal viewer: top view of the terminal (quay at the top, stack at the bottom) served by
// the ECS service under /ui/. One ES module, no dependencies, organised by the module groups
// of the B2b spec §4: api, stream, poll, model, map, panel, main. `model` and `map` are pure
// (no DOM access); `panel.render(state)` writes the side panel. The browser tests reach them
// through `window.__viewer`.

export const COLORS = {
  IDLE: "#5b8c5a",
  BUSY: "#2e7d32",
  FAULT: "#c62828",
  OFFLINE: "#6d5f8a",
  MAINTENANCE: "#ef6c00",
};

const UNKNOWN_COLOR = "#9e9e9e"; // equipment the layout names but /equipment has not reported
const QUAY_COLOR = "#1e5aa8";
const GRID_COLOR = "#e3e6ea";
const OUTLINE_COLOR = "#37474f";
const LABEL_COLOR = "#263238";
const BLOCK_FILL = "#eceff1";
const ROUTE_COLOR = "#1e5aa8";
const BOX_COLOR = "#ffffff"; // container on an AGV: light square inside the state colour
const RETRY_MS = 5000;

// ---------------------------------------------------------------------------- api

/** A non-2xx answer of the ECS service: `code` from the body (`code`, else `detail`, else
 * "HTTP <status>") and its `message` ("" when absent). */
export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function request(method, path, body) {
  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(path, init);
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const b = data && typeof data === "object" ? data : {};
    const code = typeof b.code === "string" ? b.code
      : typeof b.detail === "string" ? b.detail : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, code, typeof b.message === "string" ? b.message : "");
  }
  return data;
}

const getJson = (path) => request("GET", path);

/** "code: message" of an ApiError (just the code without a message); else the error text. */
function errorText(e) {
  if (e instanceof ApiError) return e.message ? `${e.code}: ${e.message}` : e.code;
  return String((e && e.message) || e);
}

const api = {
  layout: () => getJson("/layout"),
  equipment: () => getJson("/equipment"),
  alarms: () => getJson("/alarms"),
  status: () => getJson("/status"),
  kpis: () => getJson("/kpis"),
  release: (id) => request("POST", `/equipment/${encodeURIComponent(id)}/release`),
  resolve: (jobId, outcome) => request("POST", `/jobs/${encodeURIComponent(jobId)}/resolve`, { outcome }),
};

// ---------------------------------------------------------------------------- stream

const WS_RETRY_MIN_MS = 1000;
const WS_RETRY_MAX_MS = 10000;

const stream = {
  /** Keep a WebSocket on same-origin /events open: every message goes to `onMessage`;
   * `state.link.ws` follows the socket; a close reconnects after 1 s, doubling to at most
   * 10 s, reset on open. `onOpen()` runs on every (re)open, to resync what a gap missed. */
  start(state, onMessage, onOpen = () => {}) {
    const url = (location.protocol === "https:" ? "wss" : "ws") + "://" + location.host + "/events";
    let delay = WS_RETRY_MIN_MS;
    const connect = () => {
      const ws = new WebSocket(url);
      ws.onopen = () => {
        delay = WS_RETRY_MIN_MS;
        state.link.ws = true;
        state.dirty = true;
        onOpen();
      };
      ws.onmessage = (ev) => {
        let message;
        try {
          message = JSON.parse(ev.data);
        } catch {
          return;
        }
        onMessage(message);
      };
      ws.onclose = () => {
        state.link.ws = false;
        state.dirty = true;
        setTimeout(connect, delay);
        delay = Math.min(delay * 2, WS_RETRY_MAX_MS);
      };
    };
    connect();
  },
};

// ---------------------------------------------------------------------------- poll

const POLL_MS = 1000;
const POLL_RETRY_MS = 5000;

const poll = {
  /** One `GET /equipment`: a 200 sets `state.link.ecs` true and replaces the equipment
   * snapshot, keeping the state of a device whose WS state change arrived while the request
   * was out (the snapshot may predate it); anything else sets `state.link.ecs` false and
   * rejects. */
  async refresh(state) {
    const epoch = state.equipmentEpoch;
    let items;
    try {
      items = await api.equipment();
    } catch (e) {
      state.link.ecs = false;
      state.dirty = true;
      throw e;
    }
    state.link.ecs = true;
    for (const it of items) {
      const old = state.equipment.get(it.equipment_id);
      if (old && (state.stateEpoch.get(it.equipment_id) ?? 0) > epoch) it.state = old.state;
    }
    model.setEquipment(state, items);
  },

  /** `poll.refresh` every 1000 ms (a `setTimeout` chain), 5000 ms after a failure. */
  start(state) {
    const tick = async () => {
      let next = POLL_MS;
      try {
        await poll.refresh(state);
      } catch {
        next = POLL_RETRY_MS;
      }
      setTimeout(tick, next);
    };
    setTimeout(tick, POLL_MS);
  },
};

// ---------------------------------------------------------------------------- model

const EVENT_RING = 50;

const model = {
  /** The single viewer state object (structuredClone-able: no functions, no DOM). */
  initial() {
    return {
      layout: null,
      equipment: new Map(),
      equipmentEpoch: 0, // counts WS state changes
      stateEpoch: new Map(), // equipment id -> equipmentEpoch of its last WS state change
      alarms: [], // oldest first
      status: null,
      kpis: null,
      events: [], // newest first, at most EVENT_RING
      link: { ws: false, ecs: false, connected: null, since: null },
      selected: null,
      actions: {}, // action button key -> {busy, error, until}
      dirty: true,
    };
  },

  /** "kind|equipment_id|job_id" with "" for a null job id. */
  alarmKey(a) {
    return `${a.kind}|${a.equipment_id}|${a.job_id ?? ""}`;
  },

  /** Replace the alarm list with `items` (the GET /alarms list). */
  setAlarms(state, items) {
    state.alarms = sortAlarms(items.map(alarmOf));
    state.dirty = true;
  },

  /** Store a status (GET /status body or WS `status` message without its `type`). */
  setStatus(state, status) {
    const { type, ...rest } = status;
    state.status = rest;
    if (typeof rest.connected === "boolean") setConnected(state, rest.connected, rest.sim_time);
    state.dirty = true;
  },

  setKpis(state, kpis) {
    state.kpis = kpis;
    state.dirty = true;
  },

  /** Apply one WS /events message. */
  apply(state, message) {
    switch (message.type) {
      case "alarm": {
        const key = model.alarmKey(message);
        const rest = state.alarms.filter((a) => model.alarmKey(a) !== key);
        state.alarms = message.action === "raised" ? sortAlarms([...rest, alarmOf(message)]) : rest;
        break;
      }
      case "status":
        model.setStatus(state, message);
        break;
      case "equipment": {
        pushEvent(state, message);
        const item = state.equipment.get(message.equipment_id);
        if (message.new_state) {
          if (item) item.state = message.new_state;
          state.equipmentEpoch += 1;
          state.stateEpoch.set(message.equipment_id, state.equipmentEpoch);
        }
        break;
      }
      case "job":
        pushEvent(state, message);
        break;
      case "link":
        setConnected(state, message.connected, message.sim_time);
        break;
      default:
        return;
    }
    state.dirty = true;
  },

  /** Replace the equipment snapshot with `items` (the GET /equipment list). */
  setEquipment(state, items) {
    state.equipment = new Map(items.map((it) => [it.equipment_id, it]));
    state.dirty = true;
  },

  /** Set (or with `value` null clear) the progress/error of action button `key`. */
  setAction(state, key, value) {
    if (value) state.actions[key] = value;
    else delete state.actions[key];
    state.dirty = true;
  },

  select(state, id) {
    state.selected = id;
    state.dirty = true;
  },
};

function alarmOf(a) {
  return { kind: a.kind, equipment_id: a.equipment_id, job_id: a.job_id ?? null,
           since_sim_time: a.since_sim_time, detail: a.detail ?? "" };
}

function sortAlarms(alarms) {
  return alarms.sort((a, b) => (a.since_sim_time - b.since_sim_time) ||
    model.alarmKey(a).localeCompare(model.alarmKey(b)));
}

function setConnected(state, connected, simTime) {
  if (connected === false && state.link.connected !== false) state.link.since = simTime ?? null;
  if (connected !== false) state.link.since = null;
  state.link.connected = connected;
}

function pushEvent(state, m) {
  state.events = [{
    sim_time: m.sim_time, type: m.type, kind: m.kind ?? null, equipment_id: m.equipment_id ?? null,
    job_id: m.job_id ?? null, new_state: m.new_state ?? null, reason: m.reason ?? null,
  }, ...state.events].slice(0, EVENT_RING);
}

// ---------------------------------------------------------------------------- map

const MARGIN_PX = 20;
const STS_BAR_PX = 12;
const ASC_BAR_PX = 6;
const AGV_L_M = 8;
const AGV_W_M = 4;
const BAY_PITCH_M = 6.5;
const HIT_PAD_PX = 3;

/** Drawn width (m) of a block: its real length (bays × 6.5 m) but never wider than 80 % of
 * the spacing between neighbouring blocks, so blocks along the quay stay apart. */
function blockDrawWidth(layout, block) {
  const xs = layout.blocks.map((b) => b.tp_ws.x).sort((a, b) => a - b);
  let spacing = Infinity;
  for (let i = 1; i < xs.length; i++) spacing = Math.min(spacing, xs[i] - xs[i - 1]);
  if (!Number.isFinite(spacing) || spacing <= 0) spacing = 4 * layout.grid_cell_m;
  return Math.min(block.bays * BAY_PITCH_M, 0.8 * spacing);
}

function stsBottom(layout, sts) {
  const rows = (sts.lane_cells || []).map((c) => c[1]);
  const row = rows.length ? Math.max(...rows) : 0;
  return (row + 1) * layout.grid_cell_m;
}

function colorOf(item) {
  return (item && COLORS[item.state]) || UNKNOWN_COLOR;
}

function hatch(ctx, x, y, w, h, color) {
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  ctx.beginPath();
  for (let d = -h; d < w; d += 5) {
    ctx.moveTo(x + d, y + h);
    ctx.lineTo(x + d + h, y);
  }
  ctx.stroke();
  ctx.restore();
}

function outline(ctx, item) {
  ctx.strokeStyle = OUTLINE_COLOR;
  ctx.lineWidth = 1;
  ctx.setLineDash(item && item.state === "OFFLINE" ? [4, 3] : []);
}

function label(ctx, text, x, y, align = "center", baseline = "bottom") {
  ctx.fillStyle = LABEL_COLOR;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  ctx.fillText(text, x, y);
}

/** Screen rectangles of every drawn piece of equipment, topmost first (AGV, ASC, STS). */
function shapes(state, fit) {
  const { layout, equipment } = state;
  const { scale: k, ox, oy } = fit;
  const sx = (x) => ox + x * k;
  const sy = (y) => oy + y * k;
  const out = [];
  for (const item of equipment.values()) {
    if (item.kind !== "AGV" || !item.position) continue;
    const w = AGV_L_M * k;
    const h = AGV_W_M * k;
    out.push({ id: item.equipment_id, kind: "AGV", item,
               x: sx(item.position.x) - w / 2, y: sy(item.position.y) - h / 2, w, h });
  }
  for (const b of layout.blocks) {
    const item = equipment.get(b.asc_id);
    const bw = blockDrawWidth(layout, b);
    const left = b.tp_ws.x - bw / 2;
    const gantry = Number((item && item.extra && item.extra.gantry_position_m) ?? 0);
    const along = Math.min(Math.max(gantry, 0), b.bays * BAY_PITCH_M) * (bw / (b.bays * BAY_PITCH_M));
    const x = Math.min(Math.max(left + along, left), left + bw);
    const top = sy(b.tp_ws.y);
    out.push({ id: b.asc_id, kind: "ASC", item, block: b,
               x: sx(x) - ASC_BAR_PX / 2, y: top - 3, w: ASC_BAR_PX, h: sy(fit.worldH) - top + 3 });
  }
  for (const s of layout.sts) {
    const item = equipment.get(s.equipment_id);
    out.push({ id: s.equipment_id, kind: "STS", item, sts: s,
               x: sx(s.x) - STS_BAR_PX / 2, y: sy(0), w: STS_BAR_PX, h: stsBottom(layout, s) * k });
  }
  return out;
}

const map = {
  /** Uniform scale and offset placing the world bounding box (quay length, grid and every
   * STS/block position) inside `width × height` px with a 20 px margin. */
  fit(layout, width, height) {
    const cell = layout.grid_cell_m;
    const xs = [...layout.sts.map((s) => s.x), ...layout.blocks.map((b) => b.tp_ws.x)];
    const ys = [...layout.sts.map((s) => s.y), ...layout.blocks.map((b) => b.tp_ws.y)];
    const worldW = Math.max(layout.quay_length_m, layout.grid_cols * cell,
                            xs.length ? Math.max(...xs) + cell : 0);
    const worldH = Math.max(layout.grid_rows * cell, ys.length ? Math.max(...ys) + cell : 0);
    const scale = Math.max(0, Math.min((width - 2 * MARGIN_PX) / worldW, (height - 2 * MARGIN_PX) / worldH));
    const ox = (width - worldW * scale) / 2;
    const oy = (height - worldH * scale) / 2;
    return { scale, ox, oy, worldW, worldH };
  },

  /** Draw the whole top view of `state` onto `ctx` with the transform `fit`. */
  draw(ctx, state, fit) {
    const { layout, equipment } = state;
    const { scale: k, ox, oy, worldW, worldH } = fit;
    const cell = layout.grid_cell_m;
    const sx = (x) => ox + x * k;
    const sy = (y) => oy + y * k;

    ctx.save();
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    if (state.link.ecs === false || state.link.connected === false) ctx.globalAlpha = 0.5;

    // light grid
    ctx.strokeStyle = GRID_COLOR;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let c = 0; c <= layout.grid_cols; c++) {
      ctx.moveTo(sx(c * cell), sy(0));
      ctx.lineTo(sx(c * cell), sy(layout.grid_rows * cell));
    }
    for (let r = 0; r <= layout.grid_rows; r++) {
      ctx.moveTo(sx(0), sy(r * cell));
      ctx.lineTo(sx(layout.grid_cols * cell), sy(r * cell));
    }
    ctx.stroke();

    // quay band
    ctx.fillStyle = QUAY_COLOR;
    ctx.fillRect(sx(0), sy(0), worldW * k, (cell / 2) * k);

    // blocks with TP cell and block letter
    for (const b of layout.blocks) {
      const asc = equipment.get(b.asc_id);
      const bw = blockDrawWidth(layout, b);
      const x = sx(b.tp_ws.x - bw / 2);
      const y = sy(b.tp_ws.y);
      ctx.fillStyle = BLOCK_FILL;
      ctx.fillRect(x, y, bw * k, sy(worldH) - y);
      ctx.strokeStyle = OUTLINE_COLOR;
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.strokeRect(x, y, bw * k, sy(worldH) - y);
      label(ctx, b.block, x + (bw * k) / 2, y + (sy(worldH) - y) / 2, "center", "middle");
      const [tc, tr] = b.tp_ws_cell;
      const tx = sx(tc * cell);
      const ty = sy(tr * cell);
      ctx.strokeRect(tx, ty, cell * k, cell * k);
      if (asc && asc.extra && asc.extra.ws_tp_occupied) hatch(ctx, tx, ty, cell * k, cell * k, OUTLINE_COLOR);
    }

    const drawn = shapes(state, fit);

    // STS: bar over quay and handover lane, lane cells hatched while occupied
    for (const s of drawn.filter((d) => d.kind === "STS")) {
      if (s.item && s.item.extra && s.item.extra.handover_lane_occupied) {
        for (const [c, r] of s.sts.lane_cells) hatch(ctx, sx(c * cell), sy(r * cell), cell * k, cell * k, QUAY_COLOR);
      }
      ctx.fillStyle = colorOf(s.item);
      ctx.fillRect(s.x, s.y, s.w, s.h);
      outline(ctx, s.item);
      ctx.strokeRect(s.x, s.y, s.w, s.h);
      label(ctx, s.id, s.x + s.w + 3, s.y + s.h, "left", "bottom");
    }

    // ASC: bar across its block at the gantry position, id under the block
    for (const a of drawn.filter((d) => d.kind === "ASC")) {
      ctx.fillStyle = colorOf(a.item);
      ctx.fillRect(a.x, a.y, a.w, a.h);
      outline(ctx, a.item);
      ctx.strokeRect(a.x, a.y, a.w, a.h);
      label(ctx, a.id, a.x + a.w / 2, a.y + a.h + 2, "center", "top");
    }

    // AGV: remaining route, body, box when loaded, id above
    for (const a of drawn.filter((d) => d.kind === "AGV")) {
      const extra = a.item.extra || {};
      const route = Array.isArray(extra.route) ? extra.route : [];
      if (route.length) {
        ctx.strokeStyle = ROUTE_COLOR;
        ctx.lineWidth = 1;
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(a.x + a.w / 2, a.y + a.h / 2);
        for (const [c, r] of route) ctx.lineTo(sx((c + 0.5) * cell), sy((r + 0.5) * cell));
        ctx.stroke();
      }
      ctx.fillStyle = colorOf(a.item);
      ctx.fillRect(a.x, a.y, a.w, a.h);
      outline(ctx, a.item);
      ctx.strokeRect(a.x, a.y, a.w, a.h);
      if (extra.loaded_container_id) {
        const s = Math.min(a.w, a.h) * 0.6;
        ctx.fillStyle = BOX_COLOR;
        ctx.fillRect(a.x + (a.w - s) / 2, a.y + (a.h - s) / 2, s, s);
      }
      label(ctx, a.id, a.x + a.w / 2, a.y - 2, "center", "bottom");
    }

    ctx.restore();
  },

  /** Id of the equipment drawn at screen point (`px`, `py`), or null. */
  hitTest(state, fit, px, py) {
    for (const d of shapes(state, fit)) {
      if (px >= d.x - HIT_PAD_PX && px <= d.x + d.w + HIT_PAD_PX &&
          py >= d.y - HIT_PAD_PX && py <= d.y + d.h + HIT_PAD_PX) return d.id;
    }
    return null;
  },
};

// ---------------------------------------------------------------------------- panel

const ACTION_ERROR_MS = 5000;

const ALARM_LABELS = {
  LINK_LOST: "Verbinding verbroken",
  NEEDS_RELEASE: "Vrijgave nodig",
  UNKNOWN_OUTCOME: "Uitkomst onbekend",
};

const RELEASABLE = new Set(["NEEDS_RELEASE", "LINK_LOST"]);

const STATUS_KPIS = [
  ["done", "Gereed"],
  ["failed", "Mislukt"],
  ["open", "Open"],
  ["offline", "Offline"],
  ["needs_release", "Vrijgave nodig"],
  ["unknown_outcomes", "Uitkomst onbekend"],
];

const esc = (v) => String(v).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const fmtNum = (v) => (typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v));
const fmtTime = (t) => (typeof t === "number" ? `t=${t.toFixed(1)} s` : "");

/** Key of an action button in `state.actions`. */
const releaseKey = (eid) => `release|${eid}`;
const resolveKey = (jobId, outcome) => `resolve|${jobId}|${outcome}`;

function bannerOf(state) {
  const { link } = state;
  if (!link.ws && !link.ecs) return { cls: "lost", text: "Viewer ↔ ECS verbroken" };
  if (link.connected === false) {
    const since = fmtTime(link.since);
    return { cls: "down", text: since ? `Gateway verbroken sinds ${since}` : "Gateway verbroken" };
  }
  return { cls: "ok", text: "ECS ↔ gateway verbonden" };
}

function kpisHtml(state) {
  const rows = [];
  const st = state.status || {};
  if (typeof st.sim_time === "number") rows.push(["Simulatietijd", fmtTime(st.sim_time)]);
  for (const [k, lbl] of STATUS_KPIS) if (st[k] !== undefined) rows.push([lbl, fmtNum(st[k])]);
  for (const [k, v] of Object.entries(state.kpis || {})) rows.push([k, fmtNum(v)]);
  return rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");
}

function actionButton(state, cls, key, attrs, text) {
  const a = state.actions[key];
  const data = Object.entries(attrs).map(([k, v]) => ` data-${k}="${esc(v)}"`).join("");
  return `<button type="button" class="${cls}"${data}${a && a.busy ? " disabled" : ""}>${esc(text)}</button>`;
}

function alarmsHtml(state) {
  // the release button sits on the first NEEDS_RELEASE / LINK_LOST row of each equipment
  // (spec §5); an UNKNOWN_OUTCOME alone is answered with Voltooid / Mislukt, not a release
  const released = new Set();
  return state.alarms.map((a) => {
    const buttons = [];
    const keys = [];
    if (RELEASABLE.has(a.kind) && !released.has(a.equipment_id)) {
      released.add(a.equipment_id);
      keys.push(releaseKey(a.equipment_id));
      buttons.push(actionButton(state, "release", releaseKey(a.equipment_id),
                                { equipment: a.equipment_id }, "Vrijgeven"));
    }
    if (a.kind === "UNKNOWN_OUTCOME" && a.job_id) {
      for (const [outcome, text] of [["completed", "Voltooid"], ["failed", "Mislukt"]]) {
        keys.push(resolveKey(a.job_id, outcome));
        buttons.push(actionButton(state, "resolve", resolveKey(a.job_id, outcome),
                                  { job: a.job_id, outcome }, text));
      }
    }
    const errors = keys.map((k) => state.actions[k] && state.actions[k].error).filter(Boolean);
    const what = [a.equipment_id, a.job_id].filter(Boolean).join(" · ");
    return `<li data-key="${esc(model.alarmKey(a))}" class="alarm ${esc(a.kind.toLowerCase())}">`
      + `<div><strong>${esc(ALARM_LABELS[a.kind] || a.kind)}</strong> ${esc(what)}`
      + ` <span class="muted">sinds ${esc(fmtTime(a.since_sim_time))}</span></div>`
      + (a.detail ? `<div class="muted">${esc(a.detail)}</div>` : "")
      + (buttons.length ? `<div class="actions">${buttons.join("")}</div>` : "")
      + errors.map((e) => `<div class="error">${esc(e)}</div>`).join("")
      + "</li>";
  }).join("");
}

function eventsHtml(state) {
  return state.events.map((e) => {
    const detail = [e.job_id, e.new_state && `→ ${e.new_state}`, e.reason].filter(Boolean).join(" ");
    return `<li><span class="muted">${esc(fmtTime(e.sim_time))}</span> ${esc(e.kind || e.type)}`
      + ` ${esc(e.equipment_id || "")} ${esc(detail)}</li>`;
  }).join("");
}

function selectionHtml(state) {
  const item = state.selected ? state.equipment.get(state.selected) : null;
  if (!item) return "";
  const rows = [["Id", item.equipment_id], ["Soort", item.kind], ["Toestand", item.state],
                ["Job", item.current_job_id ?? "–"]];
  for (const [k, v] of Object.entries(item.extra || {})) {
    rows.push([k, v !== null && typeof v === "object" ? JSON.stringify(v) : String(v)]);
  }
  return rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

/** Set `el.innerHTML` only when it changes, so buttons survive renders that change nothing. */
function put(el, html) {
  if (el && el.dataset.html !== html) {
    el.innerHTML = html;
    el.dataset.html = html;
  }
}

const panel = {
  /** Write `state` into the side panel (banner, KPIs, alarms, events, selection). */
  render(state) {
    const banner = document.getElementById("banner");
    const b = bannerOf(state);
    if (banner.textContent !== b.text) banner.textContent = b.text;
    banner.className = b.cls;
    put(document.getElementById("kpis"), kpisHtml(state));
    put(document.getElementById("alarms"), alarmsHtml(state));
    put(document.getElementById("events"), eventsHtml(state));
    put(document.getElementById("selection"), selectionHtml(state));
  },
};

// ---------------------------------------------------------------------------- main

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** `getJson(path)` until it succeeds, every 5 s; `onFail(error)` after each failure. */
async function fetchWithRetry(path, onFail) {
  for (;;) {
    try {
      return await getJson(path);
    } catch (e) {
      onFail(e);
      await sleep(RETRY_MS);
    }
  }
}

function drawNotice(ctx, text) {
  ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
  label(ctx, text, 20, 20, "left", "top");
}

function main() {
  const state = model.initial();
  const viewer = { state, model, map, panel, api, stream, poll, ready: false };
  window.__viewer = viewer;

  const canvas = document.getElementById("map");
  const ctx = canvas.getContext("2d");
  let notice = "Plattegrond laden…";
  let booted = false;

  const resize = () => {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(canvas.clientWidth * dpr);
    canvas.height = Math.round(canvas.clientHeight * dpr);
    state.dirty = true;
  };
  window.addEventListener("resize", resize);
  resize();

  const frame = () => {
    if (state.dirty) {
      state.dirty = false;
      const dpr = window.devicePixelRatio || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (state.layout) {
        map.draw(ctx, state, map.fit(state.layout, canvas.clientWidth, canvas.clientHeight));
        if (booted) viewer.ready = true;
      } else {
        drawNotice(ctx, notice);
      }
      panel.render(state);
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);

  // initial snapshots (and the resync after an action); a failure leaves the old value
  const refreshAlarms = () => api.alarms().then((items) => model.setAlarms(state, items), () => {});
  const refreshStatus = () => api.status().then((st) => model.setStatus(state, st), () => {});
  const refreshKpis = () => api.kpis().then((k) => model.setKpis(state, k), () => {});

  /** One WS /events message: apply it; a `status` also refetches the KPIs. */
  viewer.onMessage = (message) => {
    model.apply(state, message);
    if (message.type === "status") refreshKpis();
  };

  const act = async (key, call) => {
    if (state.actions[key] && state.actions[key].busy) return;
    model.setAction(state, key, { busy: true, error: null, until: 0 });
    try {
      await call();
      model.setAction(state, key, null);
      await Promise.all([refreshAlarms(), refreshStatus()]);
    } catch (e) {
      const text = errorText(e);
      const until = Date.now() + ACTION_ERROR_MS;
      model.setAction(state, key, { busy: false, error: text, until });
      setTimeout(() => {
        const a = state.actions[key];
        if (a && !a.busy && a.until === until) model.setAction(state, key, null);
      }, ACTION_ERROR_MS);
    }
  };

  document.getElementById("alarms").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn || btn.disabled) return;
    if (btn.classList.contains("release")) {
      const eid = btn.dataset.equipment;
      act(releaseKey(eid), () => api.release(eid));
    } else if (btn.classList.contains("resolve")) {
      const { job, outcome } = btn.dataset;
      act(resolveKey(job, outcome), () => api.resolve(job, outcome));
    }
  });

  canvas.addEventListener("click", (ev) => {
    if (!state.layout) return;
    const rect = canvas.getBoundingClientRect();
    const fit = map.fit(state.layout, canvas.clientWidth, canvas.clientHeight);
    model.select(state, map.hitTest(state, fit, ev.clientX - rect.left, ev.clientY - rect.top));
  });

  (async () => {
    state.layout = await fetchWithRetry("/layout", (e) => {
      notice = `Plattegrond niet beschikbaar (${errorText(e)}); nieuwe poging over 5 s`;
      state.dirty = true;
    });
    state.dirty = true;
    const items = await fetchWithRetry("/equipment", () => {
      state.link.ecs = false;
      state.dirty = true;
    });
    model.setEquipment(state, items);
    state.link.ecs = true;
    await Promise.all([refreshAlarms(), refreshStatus(), refreshKpis()]);
    // live from here: events over the WS, positions by polling. Every (re)open resyncs what
    // the gap missed (equipment too: state changes sent before the socket was registered);
    // `ready` waits for the first resync, so a test acting after it is not overwritten by it.
    let live;
    const first = new Promise((resolve) => { live = resolve; });
    stream.start(state, (m) => viewer.onMessage(m), async () => {
      await Promise.all([refreshAlarms(), refreshStatus(), refreshKpis(),
                         poll.refresh(state).catch(() => {})]);
      live();
    });
    poll.start(state);
    await first;
    booted = true;
    state.dirty = true;
  })();
}

main();
