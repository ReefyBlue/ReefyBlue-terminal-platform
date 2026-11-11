"""Render a v2 terminal layout (metres) to PNG — top view, quay at the top.

Usage: uv run python docs/superpowers/references/c1-terminal/render_layout.py terminal_mv.yaml out.png
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Rectangle

from terminal_model.layout import load_terminal

ZONE_STYLE = {
    "berth": ("#1e5aa8", 0.15), "agv_lanes": ("#9e9e9e", 0.15), "agv_parking": ("#757575", 0.25),
    "transfer_ws": ("#ffb74d", 0.25), "block": ("#bdbdbd", 0.0), "truck_interchange": ("#ff9800", 0.35),
    "rail_interchange": ("#8d6e63", 0.35), "rail_terminal": ("#a1887f", 0.3), "gate": ("#ab47bc", 0.4),
    "ocr_gate": ("#8e24aa", 0.4), "tech_gate": ("#7b1fa2", 0.4), "customs": ("#ce93d8", 0.4),
    "problem_parking": ("#9e9e9e", 0.4), "oog": ("#4db6ac", 0.35), "building": ("#ffa726", 0.6),
    "maintenance_lane": ("#eeeeee", 0.6),
}


def main(src: str, out: str) -> None:
    t = load_terminal(Path(src))
    w = t.quay.length_m
    depth = max(t.depth_m, max((max(y for _, y in z.polygon) for z in t.zones), default=0))
    fig, ax = plt.subplots(figsize=(w / 60 + 2, depth / 60 + 1.5))
    for z in t.zones:
        color, alpha = ZONE_STYLE.get(z.type, ("#000000", 0.1))
        ax.add_patch(Polygon(z.polygon, closed=True, facecolor=color, alpha=alpha, edgecolor=color, lw=0.6))
        xs = [p[0] for p in z.polygon]; ys = [p[1] for p in z.polygon]
        if z.type not in ("block", "maintenance_lane", "transfer_ws", "truck_interchange"):
            ax.text((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, z.name, ha="center", va="center", fontsize=6, color="#333")
    # quay edge + STS rails
    ax.plot([0, w], [0, 0], color="#1e5aa8", lw=3)
    for y in (t.sts_rails.y_ws_m, t.sts_rails.y_ls_m):
        ax.plot([0, w], [y, y], color="#1e5aa8", lw=0.8, ls="--")
    for b in t.quay.berths:
        ax.text((b.from_m + b.to_m) / 2, -6, f"berth {b.id}", ha="center", va="top", fontsize=7, color="#1e5aa8")
    # STS: bar across the rails, lanes
    for s in t.sts:
        ax.add_patch(Rectangle((s.x0_m - 13.5, t.sts_rails.y_ws_m), 27, t.sts_rails.y_ls_m - t.sts_rails.y_ws_m,
                               facecolor="#1e5aa8", alpha=0.5, edgecolor="#0d3c73"))
        ax.text(s.x0_m, (t.sts_rails.y_ws_m + t.sts_rails.y_ls_m) / 2, s.id, ha="center", va="center", fontsize=6, color="white")
        for ln in s.lanes:
            ax.add_patch(Rectangle((ln.x_m - ln.width_m / 2, ln.y_m), ln.width_m, ln.len_m, facecolor="none", edgecolor="#0d3c73", lw=0.5))
    # drive lanes, buffers
    for dl in t.apron.drive_lanes:
        ax.annotate("", xy=(w - 5 if dl.direction == "E" else 5, dl.y_m), xytext=(5 if dl.direction == "E" else w - 5, dl.y_m),
                    arrowprops=dict(arrowstyle="->", color="#616161", lw=0.6))
    for bf in t.apron.buffers:
        ax.add_patch(Rectangle((bf.x_m - 7.4, bf.y_m - 1.5), 14.8, 3.0, facecolor="#90a4ae", edgecolor="#546e7a", lw=0.4))
    # blocks: outline, bays, reefer rows, ASC ids; racks
    for b in t.blocks:
        ax.add_patch(Rectangle((b.x0_m, b.y0_m), b.width_m, b.length_m, facecolor="#e0e0e0", edgecolor="#424242", lw=0.8))
        for i in range(1, b.bays):
            y = b.y0_m + i * b.bay_pitch_m
            ax.plot([b.x0_m, b.x0_m + b.width_m], [y, y], color="#bdbdbd", lw=0.2)
        for r in b.reefer_rows:
            ax.add_patch(Rectangle((b.x0_m + r * b.row_pitch_m, b.y0_m), b.row_pitch_m, b.length_m, facecolor="#64b5f6", alpha=0.5, lw=0))
        ax.text(b.x0_m + b.width_m / 2, b.y0_m + b.length_m / 2, b.id, ha="center", va="center", fontsize=9, color="#212121")
        ax.text(b.x0_m + b.width_m / 2, b.y0_m + 8, "WS", ha="center", fontsize=5, color="#c62828")
        ax.text(b.x0_m + b.width_m / 2, b.y1_m - 8, "LS", ha="center", fontsize=5, color="#c62828")
    for z in t.transfer_zones:
        for r in z.racks:
            ax.add_patch(Rectangle((r.x_m - 1.3, r.y_m - 6.5), 2.6, 13.0, facecolor="#ff7043" if r.reefer else "#ffb74d", edgecolor="#e65100", lw=0.4))
    for h in t.landside:
        blk = t.block(h.block)
        for i in range(h.lanes):
            x = blk.x0_m + (i + 0.5) * blk.width_m / h.lanes
            ax.plot([x, x], [h.y_m, h.y_m + h.len_m], color="#ff9800", lw=0.8)
    ax.set_xlim(-10, w + 10); ax.set_ylim(depth + 10, -20)
    ax.set_aspect("equal"); ax.set_xlabel("x (m) langs de kade"); ax.set_ylabel("y (m) landinwaarts")
    ax.set_title(f"{t.name}: kade {w:.0f} m · {len(t.sts)} STS · {len(t.blocks)} blokken · {sum(len(z.racks) for z in t.transfer_zones)} racks", fontsize=10)
    fig.tight_layout(); fig.savefig(out, dpi=130); print(out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
