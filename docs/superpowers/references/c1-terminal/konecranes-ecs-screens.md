# Konecranes ECS / FMDS / RCC — schermreferentie (praktijk)

Afgeleid van schermfoto's van een draaiende Konecranes-installatie (TEAMS ECS,
FMDS service-GUI, RCC-operatordesk) en Navis N4. Foto's staan bewust niet in de
repo; dit document is de bron voor T6 (OPC UA-variabelen) en C3 (viewer).
Alle identifiers hieronder zijn naamconventies, geen echte waarden.

## 1. Naamgeving en hiërarchie

```
Terminal
├── AGV                      (vloot; teller "AGVs 80/80", power saving "On 15/99")
├── ASC
│   ├── Substation04         (voedingsgroep; 4 substations voor 27 modules)
│   │   ├── HC 1             (per substation één HC-node, status eigen kleur)
│   │   ├── Module03
│   │   │   ├── C-AS031      (ASC waterzijde, xx1)
│   │   │   ├── C-AS032      (ASC landzijde,  xx2)
│   │   │   ├── ICZ03 Ws     (interchange zone waterzijde = onze TransferZone)
│   │   │   └── ICZ03 Ls     (interchange zone landzijde  = truck interchange)
│   │   └── Module04 … 08
│   ├── Substation12 (HC 2, Module09…16)
│   ├── Substation22 (HC 3, Module17…24, HC 4, Module25…27)
│   └── RCC                  (remote crane control desks)
├── BMS, FCS, TDS, OPC       (overige subsystemen; OPC = OPC-server node)
```

- Modules zijn genummerd 03…27 (bay-nummering op de modules-overview loopt
  van 27 links naar 03 rechts, LS boven / WS onder, met een N-pijl).
- Statuskleur per node in de boom: groen = ok, rood driehoek = alarm, paars = anders.
- **Overnemen in ons model:** block id `M003`, ASC ids `AS031`/`AS032`
  (`asc_ws_id` = `<blok>1`, `asc_ls_id` = `<blok>2`), transfer zones
  `ICZ03-WS`/`ICZ03-LS`. Locatie-logformaat `M003F10` = module 03, bay F, rij 10.

## 2. ECS "Primary terminal view" (TEAMS GUI)

Menu: AGV · ASC · QC · TP · Area · Vessel · Monitor. Zoom-slider, pan-knoppen.

Inhoud van de kaart:
- Kade met bollards (statusbalk toont "Between the plane of bollards BMxxx/BMyyy"
  voor de cursor), schepen als contour met naam, QC's als blauwe balken op rails
  met pijl ↑/↓ (laden/lossen) en een tellertje.
- AGV-paden als vloeiende bogen/lussen; AGV's als genummerde cirkels, kleur =
  status (groen rijdend, geel wachtend, cyaan/paars = speciale toestand).
- TP's (transfer points) voor elke module; modules als grijze balken met
  AS0x1/AS0x2 als kraansymbool op positie; cantilever-modules tekenen de
  zijlaadplaats ernaast.
- Statusbalk: Zoom, AGVs n/n, Power saving, X/Y (m), bollardvlak, modus
  (Remote), rijmodus (NormalDriving), tijd; knoppen ALARM (rood) / NOTIFY
  (groen) / SYSTEM (geel).
- Dock-panelen: Find and select, Command feedback messages, Alarm panel,
  Notify panel (Equipment · Message · Timestamp · Acknowledged by).

**C3-eisen:** kaart op schaal met deze lagen; cursor-coördinaat in meters;
AGV-nummering; QC-richtingpijl; drie dock-panelen (alarm/notify/feedback).

## 3. FMDS service-GUI per kraan (C-AS031)

Tabs: Status · Error · Data · Maintenance · User · System · SQL Query · KPI.
Status-subtabs: Crane · Gantry · Trolley · Hoist · Spreader. Elke pagina toont
"Last data received at" en "FMDS server time" (staleness zichtbaar!).

### 3.1 Crane
| Groep | Velden |
|---|---|
| Main Status | technical (green/yellow/red), operational (Automatic mode / Manual RCC / Sleep Mode), operating hours, operating cycles |
| Other Status | coupling RCC desk (nr), confirmed maint pos (YES/NO) |
| Work in Progress | status (bv. Finishing The Order), order status (Order Completed), order type (Drive Only Order, …), from location phys x/y/z mm + log `M003F10`, to location idem |
| Actual position / speed | gantry, trolley, hoist in mm en m/min |
| Actual load | cell 1, cell 2, avg — gross / net (kg) |
| Container & Truck | ct ISO (bv. 46G0), ct weight, ct ID, truck ID |

### 3.2 Gantry
Master + Slave (twee aandrijfzijden): technical status, operating hours,
drive pos set/actual mm + **Stack** (= bay-index), man set ‰, electrics AC/DC
(current, voltage, frequency), drive speed ‰ / m/min / rpm, enabling status
(General; Positive Direction; Negative Direction). Bogies +DA/+DB: motortemps,
hydraulics (oil temp, press, contamination, level), brakes open/closed.

### 3.3 Hoist
Spreader load cell1/cell2/avg gross/net; drive pos set/actual mm + **Tier**
(G = ground); electrics; speed; enabling; Brake 1/2 (status, opening distance,
wear, disc temps); motor/hydraulics.

### 3.4 Spreader
Spreader load, technical status, spreader ID, spreader width cmd/current
(20/40 ft), operating hours, twistlock counter; moving unit set/current:
transversal mm, lateral 1/2 mm, slew °; per hoek **+A/+B/+C/+D**: landing
sensor (landed/not landed), holding sensor (holding/not holding), twist lock
(locked/unlocked), flipper (open/close).

### 3.5 Error-tab
Active · History · Email notification · Log/Tracefile. Kolommen:
Occurred · Acknowledged · Equipment · Error No. · Errortype (W/…) · Message ·
Location (+L17) · Device (=00.M-F31-12) · User acknowledged · Op. Mode ·
Position (x,y,z) · **Job Stage** · **Job ID**. Detailregel: Errorclass, Item.

Job stages gezien: `Lowering To Pick`, `Finishing`, `Finished`.
Error levels op de alarmlijst: **R1–R4** (R1 licht/automatisch herstel … R4
hard technisch). Voorbeeldmeldingen (generiek): Heartbeat ASC↔ASC-MS missing
(R1), Temperature E-container > 45 °C (R2), Fault light barrier lane (R2),
Master out of working area (R3), Stop0 no release lane (R3), Automated moves
stopped: gate ICZ-LS opened too long (R3), Overvoltage protection failure (R4),
Discrepancy fault feedback switch crane in maintenance position (R3).

### 3.6 Modules-overview (ASC-niveau)
Alle modules naast elkaar als bay-kolommen (oranje = bezet raster), LS boven /
WS onder, kraansymbolen (cyaan rechthoek met blauwe positie) met groene
bewegingspijl, TP-rijen onderaan, AGV's als kleine groene streepjes ervoor;
onder de kaart de rode alarmlijst (Occurred · Equipment · Error no. · Error
level · Message · Location · Device).

## 4. RCC remote-operatordesk (ASC truckafhandeling)

Twee camerabeelden (lane-overzicht met groene uitlijnlijnen; "BROKEN CAMERA"
als placeholder bij uitval). Onderbalk:
- Requesting instance: ASC · CAM · HC · TOS, State-lamp; Crane: ManualRCC /
  Running; RCC: ManualTakeOver / Undefined.
- Active Position X/Y/Z/D (mm, °) + T/L1/L2; New Position idem.
- Active FATS step (Fully Automated Truck handling Sequence): schema met
  truck lane safety zone.
- Spreader Status per hoek: Landed / Holding; Slackrope; Overload.
- Order: 40 ft GET/PUT; Spreader Size 20/30/40/45; Weight; Position ICZ WS.
- Container: TOS-id, OCV; ASC id + LANE; Trailer: Ch.ID, Type (AGV/truck),
  Height, Weight.
- Control: GO · R · stop · ! ; FATS: Auto · Mon · SEMI; Trailer-type knoppen.

**C3-eisen:** exceptiepaneel per ASC met deze velden (geen video; camera-vak
als placeholder), GO/stop/release-knoppen koppelen aan `resolve_job`.

## 5. Navis N4 (TOS-zijde, alleen koppelvlak)

Truck Visits: Gate · Entered Yard · Exited Yard · Minutes In Yard · Exchange
Area (L021) · Truck Position Slot (L021D) · Truck License · Trucking Company ·
Chs Profile · Next Stage (yard) · Truck Visit Status (OK) · Priority ·
Transaction Count. Truck Visit Inspector: status COMPLETE, appointment nr,
entered/exited, truck position `Y-RWG-OUTGATE`.
**Overnemen:** landzijde truckslot-naamgeving `L021D` = interchange lane 21,
positie D; truck-visit stages voor C4.

## 6. Wat dit betekent voor T6 (OPC UA-variabelen per machine)

Minimaal per ASC/STS: `TechnicalStatus` (green/yellow/red), `OperationalMode`
(Automatic/ManualRCC/Sleep), `OperatingHours`, `OperatingCycles`,
`JobId`, `JobStage`, `OrderType`, `FromLocationPhys` (x,y,z), `FromLocationLog`,
`ToLocation…`, `GantryPos_mm`/`TrolleyPos_mm`/`HoistPos_mm` + `…Speed`,
`GantryBay`, `HoistTier`, `LoadGross/Net`, `SpreaderWidth`, `Twistlocks`
(per hoek locked/landed/holding), `Holding` (container-id), `CtIso`, `CtId`,
`TruckId`/`TrailerType`, `LastDataReceivedAt`. Per module/ICZ: rack-bezetting,
`GateOpen`, lane-vrijgave. Per AGV: positie, snelheid, `Load`, `Battery`,
`PowerSaving`. Alarmen: Occurred, ErrorNo, Level R1–R4, Message, Location,
Device, Position, JobStage, JobId, Acknowledged(by).
