# Task 7 report — deploy/ (Dockerfile, compose, env, scenario, collector, docs)

Status: DONE · Commit: 614dd29 `feat(deploy): Docker Compose stack for the QNAP and soak collector`

## Files
- `deploy/Dockerfile` — builder `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` (`uv sync --frozen --no-dev --all-packages`, flag verified in `uv sync --help`, uv 0.11.32; `UV_PYTHON=/usr/local/bin/python3.12`, `UV_PYTHON_DOWNLOADS=never`, cache mount), runtime `python:3.12-slim`, both `--platform=linux/amd64`; copies `/app/.venv`, `pyproject.toml uv.lock terminal.yaml`, `packages/` (editable workspace installs point at `/app/packages/*/src`), `deploy/scenarios/`; user `app` (uid/gid 1000), `/data/soak` pre-created and owned by `app` (named volume inherits it); `WORKDIR /app`, `PATH=/app/.venv/bin:$PATH`, `PYTHONUNBUFFERED=1`.
- `deploy/docker-compose.yml` — five services from an `x-service` anchor (same `image: terminal-platform:local` + `build` on every service so none tries a registry pull; `init: true`; json-file 20m×5; `restart: unless-stopped`). `soak`: `restart: on-failure`, `stop_grace_period: 60s`, no healthcheck, volume `soakdata:/data/soak`. `sim` bind-mounts `./scenarios:/app/deploy/scenarios:ro`. Chain sim→gateway→chaos→ecs→soak with `service_healthy`. Only `ecs` publishes `8000:8000`.
- `deploy/.env.example` (exact keys/values from brief, with Dutch comments), `deploy/scenarios/soak.yaml`, `deploy/collect.sh` (executable), `deploy/README.md` (short, points to root README), `.dockerignore` (`.git .venv out tests docs .superpowers`, plus `__pycache__`, `*.pyc`, `.pytest_cache`, `deploy/.env`).
- `README.md` — new section "Draaien op de NAS (B2c)" between "Webviewer (B2b)" and "Verder": services table, settings table, SSH steps, Container Station route, watching, finishing with `./collect.sh`, auto-release note (§6), limitations (§7) + amd64-only.
- `packages/sim/src/terminal_sim/serve.py` — **additive** `--speed-factor` override (dataclasses.replace; must be > 0). Reason: the brief requires `SPEED_FACTOR` in `.env.example`, but nothing consumed it; without this flag the key would be dead. Test in `tests/sim/test_serve.py`. No change under `packages/ecs/src` → no sha re-baseline.

## Deviations / decisions
1. **AUTO_RELEASE mapping** — the suggested compose-time `${AUTO_RELEASE:+--auto-release}` would also emit the flag for `AUTO_RELEASE=0` (non-empty). Instead `AUTO_RELEASE` is passed into the ecs container (`${AUTO_RELEASE-1}`: unset → 1, empty/0 → off) and the shell-form command does `case "$AUTO_RELEASE" in 1|true|yes) set -- --auto-release ;; *) set -- ;; esac; exec python … "$@"`. Verified by test (renders config for 1/0/empty and executes the rendered script with `exec`→`echo`). `collect.sh` uses the same rule (no .env/key → 1; else not 1/true/yes → `--manual`).
2. **ECS healthcheck = TCP connect to 8000**, not `GET /health` (spec table says `/health` 200): `/health` returns 503 whenever the gateway link is down, i.e. during every chaos cut; an HTTP check would flip the ECS unhealthy each cut. Documented in compose comment and README. `start_period: 60s`.
3. chaos healthcheck TCP 4840: verified LinkProxy keeps its listener during a cut (accept + reset), so the check stays green during cuts.
4. collect.sh: `docker compose ps -a -q <svc>` + `docker inspect -f '{{.RestartCount}}'` (−1 if no container); writes `restarts.json` locally, pipes it into the volume via `docker compose run --rm --no-deps -T soak sh -c 'cat > /data/soak/restarts.json'`, copies `samples.jsonl` out the same way, then runs the report exactly as briefed (`--no-deps -T` added so no dependency is started and stdout is clean) → `soak-report.md`. README warns to collect before `docker compose down` (restart counts are lost on down).

## Tests
- `tests/deploy/test_compose.py` (11): env keys exact; soak.yaml loads, name soak, discharge 0.5, speed 1, faults on all `Kind`s; .dockerignore; compose config (skip if no `docker`): five services, logging, restart policies, healthchecks, only ecs 8000:8000, depends_on chain service_healthy, key command flags, soak stop_grace_period + volume; AUTO_RELEASE 1/0/empty → flag; `collect.sh` with a fake `docker` on PATH (restarts.json content, volume copy, samples, report command, `--manual` for none/1/0/empty).
- `uv run pytest tests/deploy -q` → 11 passed (failing first: 7 failed before deploy/ existed). `uv run pytest -q` → 638 passed, 16 deselected. `uv lock` unchanged (no new deps).
- Not runnable here: image build (no daemon; `docker buildx build --check` also needs the daemon), real stack run. Risk points for the first NAS build: venv interpreter symlink (`/usr/local/bin/python3.12`, present in both images), Container Station handling of `context: ..`.

## Rendered `docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config`
```yaml
name: terminal-platform
services:
  chaos:
    restart: unless-stopped
    command:
      - python
      - -m
      - terminal_soak.chaos
      - --listen
      - "4840"
      - --target
      - gateway:4840
      - --every
      - "14400"
      - --for
      - "30"
      - --host
      - 0.0.0.0
    image: terminal-platform:local
    init: true
    logging:
      driver: json-file
      options:
        max-file: "5"
        max-size: 20m
    networks:
      default: null
    build:
      context: /home/claude/terminal-platform
      dockerfile: deploy/Dockerfile
      platforms:
        - linux/amd64
    depends_on:
      gateway:
        condition: service_healthy
        required: true
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import socket; socket.create_connection(('127.0.0.1', 4840), 2)
      timeout: 5s
      interval: 10s
      retries: 6
      start_period: 10s
  ecs:
    restart: unless-stopped
    command:
      - sh
      - -c
      - case "$$AUTO_RELEASE" in 1|true|yes) set -- --auto-release ;; *) set -- ;;
        esac; exec python -m terminal_ecs_service.serve --gateway opc.tcp://chaos:4840
        --layout /app/terminal.yaml --host 0.0.0.0 --port 8000 "$$@"
    environment:
      AUTO_RELEASE: "1"
    image: terminal-platform:local
    init: true
    logging:
      driver: json-file
      options:
        max-file: "5"
        max-size: 20m
    networks:
      default: null
    build:
      context: /home/claude/terminal-platform
      dockerfile: deploy/Dockerfile
      platforms:
        - linux/amd64
    depends_on:
      chaos:
        condition: service_healthy
        required: true
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import socket; socket.create_connection(('127.0.0.1', 8000), 2)
      timeout: 5s
      interval: 15s
      retries: 4
      start_period: 1m0s
    ports:
      - mode: ingress
        target: 8000
        published: "8000"
        protocol: tcp
  gateway:
    restart: unless-stopped
    command:
      - python
      - -m
      - terminal_gateway.serve
      - --config
      - /app/terminal.yaml
      - --upstream-host
      - sim
      - --base-port
      - "48400"
      - --port
      - "4840"
      - --host
      - 0.0.0.0
      - --n-sts
      - "2"
      - --n-agv
      - "6"
    image: terminal-platform:local
    init: true
    logging:
      driver: json-file
      options:
        max-file: "5"
        max-size: 20m
    networks:
      default: null
    build:
      context: /home/claude/terminal-platform
      dockerfile: deploy/Dockerfile
      platforms:
        - linux/amd64
    depends_on:
      sim:
        condition: service_healthy
        required: true
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import socket; socket.create_connection(('127.0.0.1', 4840), 2)
      timeout: 5s
      interval: 10s
      retries: 6
      start_period: 30s
  sim:
    restart: unless-stopped
    command:
      - python
      - -m
      - terminal_sim.serve
      - /app/deploy/scenarios/soak.yaml
      - --layout
      - /app/terminal.yaml
      - --autostart
      - --speed-factor
      - "1"
      - --base-port
      - "48400"
      - --host
      - 0.0.0.0
    image: terminal-platform:local
    init: true
    logging:
      driver: json-file
      options:
        max-file: "5"
        max-size: 20m
    networks:
      default: null
    volumes:
      - type: bind
        source: /home/claude/terminal-platform/deploy/scenarios
        target: /app/deploy/scenarios
        read_only: true
        bind: {}
    build:
      context: /home/claude/terminal-platform
      dockerfile: deploy/Dockerfile
      platforms:
        - linux/amd64
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import socket; socket.create_connection(('127.0.0.1', 48400), 2)
      timeout: 5s
      interval: 10s
      retries: 6
      start_period: 30s
  soak:
    restart: on-failure
    command:
      - python
      - -m
      - terminal_soak.driver
      - --ecs
      - http://ecs:8000
      - --layout
      - /app/terminal.yaml
      - --hours
      - "72"
      - --out
      - /data/soak
      - --low-water
      - "40"
      - --batch
      - "60"
    image: terminal-platform:local
    init: true
    logging:
      driver: json-file
      options:
        max-file: "5"
        max-size: 20m
    networks:
      default: null
    stop_grace_period: 1m0s
    volumes:
      - type: volume
        source: soakdata
        target: /data/soak
        volume: {}
    build:
      context: /home/claude/terminal-platform
      dockerfile: deploy/Dockerfile
      platforms:
        - linux/amd64
    depends_on:
      ecs:
        condition: service_healthy
        required: true
networks:
  default:
    name: terminal-platform_default
volumes:
  soakdata:
    name: terminal-platform_soakdata
x-service:
  build:
    context: ..
    dockerfile: deploy/Dockerfile
    platforms:
      - linux/amd64
  image: terminal-platform:local
  init: true
  logging:
    driver: json-file
    options:
      max-file: "5"
      max-size: 20m
  restart: unless-stopped
```
