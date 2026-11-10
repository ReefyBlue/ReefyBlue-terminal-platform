import random, json, sys
from dataclasses import replace
from pathlib import Path
from terminal_runner.run import make_allocator, make_workorders, prefill
from terminal_ecs.dispatcher import Dispatcher
from terminal_kpi.kpi import KpiCollector
from terminal_sim.world import World
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_model.types import Kind, State

ROOT = Path(".")
scenario = replace(load_scenario(ROOT/"scenarios/baseline.yaml"), seed=1)
layout = load_layout(ROOT/"terminal.yaml")
world = World(layout, scenario)
alloc = make_allocator(layout)
orders = make_workorders(scenario, layout, alloc, random.Random(1))
try:
    disp = Dispatcher(world.port, alloc, auto_release=True)
except TypeError:
    disp = Dispatcher(world.port, alloc)
kpi = KpiCollector(world.port, disp, faults_total=world.faults_total)
prefill(world.yard, orders); disp.submit(orders); disp.tick()
T = float(sys.argv[1]) if len(sys.argv) > 1 else 1800.0
t = 0.0
while t < T:
    t += 60; world.run(until=t); disp.tick(); kpi.sample()
snaps = {e: world.port.snapshot(e) for e in world.port.equipment_ids()}
st = disp.status(); k = kpi.snapshot(t)

W, H = 1000, 520; sx = W/420; oy = 60
def X(x): return 10 + x*sx
def Y(y): return oy + y*sx
col = {State.IDLE:"#4caf50", State.BUSY:"#1e88e5", State.FAULT:"#e53935"}
o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="Helvetica, Arial" font-size="12">',
     f'<rect width="{W}" height="{H}" fill="#f7f7f7"/>',
     f'<text x="12" y="22" font-size="16" font-weight="bold">Terminal fase 1 — baseline seed 1 — sim-tijd {t/60:.0f} min</text>',
     f'<rect x="{X(0)}" y="{Y(-8)}" width="{400*sx}" height="{8*sx}" fill="#90caf9"/><text x="{X(2)}" y="{Y(-2)}" fill="#0d47a1">kade / schip</text>']
# grid
for c in range(layout.grid_cols):
    for r in range(layout.grid_rows):
        o.append(f'<rect x="{X(c*20)}" y="{Y(r*20)}" width="{20*sx}" height="{20*sx}" fill="none" stroke="#ddd"/>')
for s in layout.sts:
    for cell in s.lane_cells:
        o.append(f'<rect x="{X(cell[0]*20)}" y="{Y(cell[1]*20)}" width="{20*sx}" height="{20*sx}" fill="#fff3e0" stroke="#ffb74d"/>')
for b in layout.blocks:
    cx, cy = b.tp_ws_cell
    o.append(f'<rect x="{X(cx*20)}" y="{Y(cy*20)}" width="{20*sx}" height="{20*sx}" fill="#e8f5e9" stroke="#66bb6a"/>')
    o.append(f'<rect x="{X(cx*20-30)}" y="{Y(125)}" width="{80*sx}" height="{30*sx}" fill="#eceff1" stroke="#90a4ae"/>')
    o.append(f'<text x="{X(cx*20-26)}" y="{Y(143)}" font-weight="bold">blok {b.block}</text>')
for eid, s in snaps.items():
    c = col.get(s.state, "#9e9e9e"); x, y = X(s.position.x), Y(s.position.y)
    if s.kind is Kind.STS:
        o.append(f'<rect x="{x-14}" y="{y-6}" width="28" height="28" fill="{c}" stroke="#333"/><text x="{x-20}" y="{y+40}" font-size="11">{eid}</text>')
    elif s.kind is Kind.AGV:
        box = s.extra.get("loaded_container_id")
        o.append(f'<rect x="{x-9}" y="{y-6}" width="18" height="12" fill="{c}" stroke="#333"/>'+(f'<rect x="{x-7}" y="{y-10}" width="14" height="5" fill="#ff9800"/>' if box else '')+f'<text x="{x-14}" y="{y+22}" font-size="10">{eid[-2:]}</text>')
    else:
        o.append(f'<circle cx="{x}" cy="{y}" r="9" fill="{c}" stroke="#333"/><text x="{x+12}" y="{y+4}" font-size="11">{eid}{" @"+s.current_job_id if s.current_job_id else ""}</text>')
# legend & kpis
o.append(f'<rect x="{X(0)}" y="{H-120}" width="{W-20}" height="110" fill="#fff" stroke="#bbb"/>')
leg = [("IDLE","#4caf50"),("BUSY","#1e88e5"),("FAULT","#e53935"),("box op AGV","#ff9800")]
for i,(n,cc) in enumerate(leg):
    o.append(f'<rect x="{20+i*110}" y="{H-108}" width="12" height="12" fill="{cc}"/><text x="{36+i*110}" y="{H-97}">{n}</text>')
lines = [f"orders: open {st['open']}  klaar {st['done']}  gefaald {st['failed']}  actieve jobs {st['active_jobs']}  herplanningen {st['replans']}",
         f"moves/uur totaal {k['moves_per_hour_total']:.1f}   STS-01 {k['moves_per_hour_STS-01']:.1f}   STS-02 {k['moves_per_hour_STS-02']:.1f}   bezetting STS {k['utilisation_STS']:.0%}  AGV {k['utilisation_AGV']:.0%}  ASC {k['utilisation_ASC']:.0%}",
         f"AGV wacht bij STS {k['agv_wait_sts_s_mean']:.0f} s   wacht bij TP {k['agv_wait_tp_s_mean']:.0f} s   rijtijd {k['agv_travel_s_mean']:.0f} s   energie {k['energy_kwh_per_move']:.2f} kWh/move"]
for i,l in enumerate(lines):
    o.append(f'<text x="20" y="{H-75+i*20}">{l}</text>')
o.append('</svg>')
Path("/tmp/snap/snapshot.svg").write_text("\n".join(o))
print(json.dumps({"t":t,"status":st}, indent=1)[:600])
