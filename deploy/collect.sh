#!/bin/sh
# Na de soak-run: herstarttellers + meetpunten ophalen en het rapport maken.
#   cd deploy && ./collect.sh            -> restarts.json, samples.jsonl, soak-report.md (hier)
# Werkt ook als de soak-container al gestopt is (normaal einde van --hours).
set -eu
cd "$(dirname "$0")"

# AUTO_RELEASE zoals compose het ziet: .env (indien aanwezig), anders 1.
AUTO_RELEASE_VALUE=1
if [ -f .env ]; then
  line=$(grep -E '^[[:space:]]*AUTO_RELEASE=' .env | tail -n 1 || true)
  if [ -n "$line" ]; then
    AUTO_RELEASE_VALUE=$(printf '%s' "${line#*=}" | tr -d '[:space:]"'"'")
  fi
fi

# 1. RestartCount per service -> restarts.json  ({"sim": 0, ...}; -1 = container niet gevonden)
printf '{' > restarts.json
sep=''
for svc in sim gateway chaos ecs soak; do
  id=$(docker compose ps -a -q "$svc" | head -n 1)
  count=-1
  if [ -n "$id" ]; then
    count=$(docker inspect -f '{{.RestartCount}}' "$id")
  fi
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
