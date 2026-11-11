# B2b viewer — ontwerpinput (10-11-2026)

Referenties van de gebruiker ("check these for how it needs to look"):

- `ecs-screen-reference.png` (links): ECS/TOS-schermbeeld, bovenaanzicht. Kade bovenaan met schip; STS-kranen als verticale blauwe balken met kraannummer (STS08–STS14) en pijl voor werkrichting; AGV's als groene rechthoeken (met box) op rijpaden met bochten; parkeerplekken `P`; onderaan de stackblokken (A22–A32) met de ASC-positie als balk over het blok (ASC23W…). Lijntekening, palet blauw/groen/grijs/wit. Rechts: luchtfoto ter vergelijking.
- `process-flow-reference.png`: lagen berth → internal path → yard → external path met import-/exportstappen (QC → AGV → ASC → slot). Past op onze stage-keten; bruikbaar als legenda/zijpaneel.
- `snapshot_render_prototype.py`: eerste SVG-render van de layout uit een in-process run (zelfde oriëntatie); vertrekpunt voor de Canvas-tekening.

Stijlbesluiten voor B2b: bovenaanzicht in dezelfde oriëntatie (kade boven, stack onder); kranen als balken over kade + baan; AGV's als rechthoeken met boxindicatie en id; blokken met TP-cel en ASC-balk; toestandskleuren IDLE/BUSY/FAULT/OFFLINE; alarmen- en KPI-paneel naast de plattegrond.
