# deploy/ — de stack op de NAS (Plan B2c)

Volledige uitleg (services, instellingen, meekijken, beperkingen): README in de repo-root,
sectie **"Draaien op de NAS (B2c)"**.

Kort:

```bash
cd deploy
cp .env.example .env              # SCENARIO, SPEED_FACTOR, SOAK_HOURS, CUT_EVERY_S, CUT_FOR_S, AUTO_RELEASE, LOW_WATER, BATCH
docker compose up -d --build      # sim -> gateway -> chaos -> ecs (:8000) -> soak
# Container Station: Applications -> Create met docker-compose.yml, applicatiepad = deze map
```

- Viewer: `http://<nas>:8000/ui/`; logs: `docker compose logs -f ecs`;
  meetpunten: `docker compose exec soak tail -f /data/soak/samples.jsonl`.
- Na de run (of na `docker compose stop soak`): `./collect.sh` → `restarts.json`, `samples.jsonl`,
  `soak-report.md` in deze map. Pas daarna `docker compose down`.

| bestand | rol |
|---|---|
| `Dockerfile` | één image (uv-builder → `python:3.12-slim`, `linux/amd64`, `USER app`); build-context is de repo-root |
| `docker-compose.yml` | de vijf services, healthchecks, volume `soakdata` |
| `.env.example` | instellingen met standaardwaarden |
| `scenarios/soak.yaml` | storingen als `scenarios/faults.yaml`, `discharge_share: 0.5`, `speed_factor: 1` (bind-mount) |
| `collect.sh` | herstarttellers + meetpunten ophalen, rapport maken |
