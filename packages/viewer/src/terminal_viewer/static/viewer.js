// Terminal viewer: top view of the terminal (quay at the top, stack at the bottom) served by
// the ECS service under /ui/. One ES module, no dependencies, organised by the module groups
// of the B2b spec §4: api, stream, poll, model, map, panel, main. `model` and `map` are pure
// (no DOM access); the browser tests reach them through `window.__viewer`.

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

async function getJson(path) {
  const resp = await fetch(path, { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error(`${path}: HTTP ${resp.status}`);
  return resp.json();
}

// ---------------------------------------------------------------------------- model

const model = {
  /** The single viewer state object. */
  initial() {
    return {
      layout: null,
      equipment: new Map(),
      alarms: [],
      status: null,
      kpis: null,
      events: [],
      link: { ws: false, ecs: false, connected: null },
      selected: null,
      dirty: true,
    };
  },

  /** Apply one WS /events message; the message handling arrives with the panel (Task 4). */
  apply(state, message) {
    state.dirty = true;
  },

  /** Replace the equipment snapshot with `items` (the GET /equipment list). */
  setEquipment(state, items) {
    state.equipment = new Map(items.map((it) => [it.equipment_id, it]));
    state.dirty = true;
  },
};

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
    if (state.link.ecs === false) ctx.globalAlpha = 0.5;

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
  const viewer = { state, model, map, ready: false };
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
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);

  (async () => {
    state.layout = await fetchWithRetry("/layout", (e) => {
      notice = `Plattegrond niet beschikbaar (${e.message}); nieuwe poging over 5 s`;
      state.dirty = true;
    });
    state.dirty = true;
    const items = await fetchWithRetry("/equipment", () => {
      state.link.ecs = false;
      state.dirty = true;
    });
    model.setEquipment(state, items);
    state.link.ecs = true;
    booted = true;
  })();
}

main();
