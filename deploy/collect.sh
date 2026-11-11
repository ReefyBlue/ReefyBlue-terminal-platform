#!/bin/sh
# Na de soak-run: herstarttellers + meetpunten ophalen en het rapport maken.
#   cd deploy && ./collect.sh            -> restarts.json, samples.jsonl, soak-report.md (hier)
# Werkt ook als de soak-container al gestopt is (normaal einde van --hours).
set -eu
# pipefail where the shell has it (bash, busybox ash, dash >= 0.5.13; older dash lacks it)
if (set -o pipefail) 2>/dev/null; then set -o pipefail; fi
cd "$(dirname "$0")"

# AUTO_RELEASE zoals compose het ziet: .env (indien aanwezig), anders 1.
AUTO_RELEASE_VALUE=1
if [ -f .env ]; then
  line=$(grep -E '^[[:space:]]*AUTO_RELEASE=' .env | tail -n 1 || true)
  if [ -n "$line" ]; then
    AUTO_RELEASE_VALUE=$(printf '%s' "${line#*=}" | tr -d '[:space:]"'"'")
  fi
fi

# 1. RestartCount per service -> restarts.json  ({"sim": 0, ...}). Een service zonder container
#    ontbreekt in het bestand; het rapport meldt hem dan als "niet gemeten".
printf '{' > restarts.json
sep=''
for svc in sim gateway chaos ecs soak; do
  id=$(docker compose ps -a -q "$svc" | head -n 1)
  if [ -z "$id" ]; then
    echo "let op: geen container voor $svc (niet gemeten)" >&2
    continue
  fi
  count=$(docker inspect -f '{{.RestartCount}}' "$id")
  printf '%s"%s": %s' "$sep" "$svc" "$count" >> restarts.json
  sep=', '
done
printf '}\n' >> restarts.json
echo "restarts: $(cat restarts.json)"

# 2. restarts.json in het volume zetten, samples.jsonl eruit halen (--no-deps: start niets anders).
docker compose run --rm --no-deps -T soak sh -c 'cat > /data/soak/restarts.json' < restarts.json
docker compose run --rm --no-deps -T soak cat /data/soak/samples.jsonl > samples.jsonl
echo "samples: $(wc -l < samples.jsonl) meetpunten -> samples.jsonl"

# 3. Rapport (in de image). AUTO_RELEASE anders dan 1 -> criterium 5 is handmatig.
manual=''
case "$AUTO_RELEASE_VALUE" in
  1|true|yes) ;;
  *) manual='--manual' ;;
esac
docker compose run --rm --no-deps -T soak \
  python -m terminal_soak.report /data/soak/samples.jsonl --restarts /data/soak/restarts.json $manual \
  > soak-report.md
echo "rapport: $(pwd)/soak-report.md"
