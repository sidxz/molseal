#!/usr/bin/env bash
# Milestone 2 driver: every step in order, stops at the first failure.
#   bash bench/run_m2.sh 2>&1 | tee bench/results/run_m2_500000.log
#   STEP=6 bash bench/run_m2.sh          # resume from step 6
#   STEPS="2 5 6 7 8 9 10" bash bench/run_m2.sh   # exactly these steps (reuses the existing loads)
# Steps 3 and 4 reset their own database volume, progress file, sidecar and snapshots; no other step does.
set -euo pipefail
cd "$(dirname "$0")/.."

N=${N:-500000}
STEP=${STEP:-1}
STEPS=${STEPS:-$(seq "$STEP" 10)}
BC=(docker compose -f bench/compose.yml)
SLICE=bench/data/slice_${N}.tsv
DB=postgresql+psycopg://molseal:molseal@localhost:5441/molseal
KEY=bench/results/bench.key
SNAP=bench/data/snap
mkdir -p bench/results

TENANT_FILE=bench/results/tenant_${N}.txt      # one tenant per N: a 1M load never updates the 500k rows
[ -f "$TENANT_FILE" ] || python3 -c 'import uuid; print(uuid.uuid4())' > "$TENANT_FILE"
TENANT=$(cat "$TENANT_FILE")
[ -f "$KEY" ] || uv run molseal keygen "$KEY"
MS=(--db "$DB" --key "$KEY" --tenant "$TENANT" --snapshot-dir "$SNAP")

# memory sampler for the whole run (appends, so a resumed run keeps the earlier samples)
( while true; do date '+%H:%M:%S'; docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}'; sleep 5; done ) \
  >> "bench/results/mem_${N}.log" 2>&1 &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

log() { echo "=== $(date '+%F %T') $*"; }
wait_ready() {   # service user compose-cmd...; TCP only, so initdb's temp server never counts
  local svc=$1 user=$2; shift 2
  for _ in $(seq 1 120); do
    "$@" exec -T "$svc" pg_isready -h 127.0.0.1 -U "$user" >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "$svc not ready" >&2; return 1
}

step1() { uv run python -m bench.fetch_chembl --n 10000; uv run python -m bench.fetch_chembl --n "$N"; }
step2() {   # fresh container with log_statement=all, so `docker compose logs db` holds only this run
  MOLSEAL_LOG_STATEMENT=all docker compose up -d --force-recreate db; wait_ready db molseal docker compose
  uv run python -m bench.gate
  docker compose stop db                         # the next plain `up` recreates it with log_statement=none
}
reset_volume() {   # profile service volume: stop the service and drop its data, so a load always starts empty
  "${BC[@]}" --profile "$1" rm -sf "$2"
  docker volume rm -f "bench_$3" >/dev/null
}
step3() {
  reset_volume molseal molseal-db molseal-pgdata
  rm -f "${SLICE}.progress.json" "${SLICE}.ids.tsv"; rm -rf "$SNAP"
  "${BC[@]}" --profile molseal up -d; wait_ready molseal-db molseal "${BC[@]}" --profile molseal
  uv run python -m bench.load_molseal --slice "$SLICE" "${MS[@]}"
}
step4() {
  reset_volume cartridge cartridge-db cartridge-pgdata
  "${BC[@]}" --profile cartridge up -d; wait_ready cartridge-db bench "${BC[@]}" --profile cartridge
  local enc; enc=$("${BC[@]}" --profile cartridge exec -T cartridge-db psql -U bench -tAc 'SHOW server_encoding')
  [ "$enc" = UTF8 ] || { echo "cartridge DB encoding is $enc; run: ${BC[*]} --profile cartridge down -v" >&2; return 1; }
  uv run python -m bench.load_cartridge --slice "$SLICE"
}
step5() { uv run python -m bench.queries --slice "$SLICE"; }
step6() {
  "${BC[@]}" --profile molseal --profile cartridge up -d
  wait_ready molseal-db molseal "${BC[@]}" --profile molseal; wait_ready cartridge-db bench "${BC[@]}" --profile cartridge
  uv run python -m bench.parity --n "$N" "${MS[@]}"
}
step7() {
  rm -f "bench/results/timings_${N}_"*.json      # --report reads only this run's files
  "${BC[@]}" --profile molseal stop
  "${BC[@]}" --profile cartridge up -d
  "${BC[@]}" --profile cartridge restart cartridge-db; wait_ready cartridge-db bench "${BC[@]}" --profile cartridge
  uv run python -m bench.run_bench --n "$N" --side cartridge --mode cold --where vm
  uv run python -m bench.run_bench --n "$N" --side cartridge --mode warm --where vm --prewarm
}
step8() {
  "${BC[@]}" --profile cartridge stop
  "${BC[@]}" --profile molseal up -d; wait_ready molseal-db molseal "${BC[@]}" --profile molseal
  "${BC[@]}" --profile bench build bench          # the image bakes bench/ in; pick up any fix
  for mode in cold warm; do
    "${BC[@]}" --profile molseal --profile bench run --rm bench -m bench.run_bench --n "$N" --side molseal \
      --mode "$mode" --where vm --tenant "$TENANT" --key /app/bench/results/bench.key
  done
}
step9() { uv run python -m bench.run_bench --n "$N" --side molseal --mode warm --where host "${MS[@]}"; }
step10() {
  uv run python -m bench.run_bench --n "$N" --report
  uv run python -m bench.leak leak-report "${SLICE}.ids.tsv" --db "$DB" --tenant "$TENANT" | tee "bench/results/leak_${N}.json"
}

for i in $STEPS; do log "step $i"; "step$i"; done
log "done"
