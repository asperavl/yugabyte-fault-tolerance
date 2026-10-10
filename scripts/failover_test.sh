#!/usr/bin/env bash
# ==============================================================================
# scripts/failover_test.sh: Fault-Injection Orchestrator
# Runs repeatable 180-second experiment:
# Phase 1: 0 - 60s:   Baseline steady-state traffic
# Phase 2: 60 - 120s: Stop yb2 (observe RF3 partition & Raft leader failover)
# Phase 3: 120 - 180s: Restart yb2 (observe node rejoin & latency recovery)
# Then runs verify_integrity.py to audit acknowledged writes.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

NUM_TRIALS="${1:-1}"
DURATION=180
STOP_TIME=60
START_TIME=120

export UV_LINK_MODE=copy
echo "-> Ensuring Python dependencies are synced..."
uv sync

mkdir -p results

echo "============================================================"
echo " [Experiment] YugabyteDB Node Failure & Recovery Harness"
echo " Planned Trials: ${NUM_TRIALS}"
echo " Schedule per Trial: Total ${DURATION}s (Stop at ${STOP_TIME}s, Restart at ${START_TIME}s)"
echo "============================================================"

for TRIAL in $(seq 1 "${NUM_TRIALS}"); do
    echo ""
    echo "============================================================"
    echo " >>> Starting Trial ${TRIAL} of ${NUM_TRIALS} <<<"
    echo "============================================================"

    # Ensure all nodes are running before starting trial
    echo "-> Ensuring cluster is running and healthy..."
    docker compose up -d
    
    # Wait for yb2 in case it was down from prior run
    for NODE in yb1 yb2 yb3; do
        while [ "$(docker inspect --format='{{json .State.Health.Status}}' "${NODE}" 2>/dev/null || echo 'starting')" != '"healthy"' ]; do
            echo "Waiting for ${NODE}..."
            sleep 2
        done
    done

    # Reset and seed database between trials
    echo "-> Resetting benchmark dataset for Trial ${TRIAL}..."
    docker compose exec -T yb1 bin/ysqlsh -h yb1 -U yugabyte -d yugabyte -f - < "${ROOT_DIR}/sql/seed.sql"

    CSV_OUTPUT="results/trial-${TRIAL}.csv"
    ACK_OUTPUT="results/ack_writes_trial_${TRIAL}.json"
    META_OUTPUT="results/trial-${TRIAL}_meta.json"
    rm -f "${CSV_OUTPUT}" "${ACK_OUTPUT}"

    # Start workload generator in background
    echo "-> Launching loadgen.py in background..."
    T_START=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")
    uv run loadgen.py \
        --rate 50 \
        --read-ratio 0.7 \
        --duration "${DURATION}" \
        --output "${CSV_OUTPUT}" \
        --ack-output "${ACK_OUTPUT}" &
    LOADGEN_PID=$!

    echo "Workload running under PID ${LOADGEN_PID}."
    
    # Wait until workload has connected and started writing
    echo -n "Waiting for workload to establish active connection..."
    while [ ! -s "${CSV_OUTPUT}" ]; do
        sleep 0.5
        if ! kill -0 "${LOADGEN_PID}" 2>/dev/null; then
            echo " ERROR: loadgen.py failed to start!"
            wait "${LOADGEN_PID}"
            exit 1
        fi
    done
    echo " [CONNECTED]"

    echo "Phase 1: Baseline operations (waiting ${STOP_TIME} seconds)..."
    sleep "${STOP_TIME}"

    # Fault Injection: Stop yb2
    T_INJECT=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")
    echo "-> [FAULT INJECTION] Stopping node yb2 at ${T_INJECT}..."
    docker compose stop yb2
    echo "Node yb2 stopped. Observing degraded cluster behavior..."

    # Wait during outage
    OUTAGE_DURATION=$((START_TIME - STOP_TIME))
    sleep "${OUTAGE_DURATION}"

    # Recovery: Restart yb2
    T_RECOVER=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")
    echo "-> [RECOVERY] Restarting node yb2 at ${T_RECOVER}..."
    docker compose start yb2

    # Wait for remaining duration
    RECOVERY_DURATION=$((DURATION - START_TIME))
    echo "Monitoring cluster recovery and stabilization (${RECOVERY_DURATION} seconds)..."
    
    # Wait for workload generator to conclude
    wait ${LOADGEN_PID} || true
    T_END=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")

    echo "-> Workload completed."

    # Record experiment timestamps metadata
    cat <<EOF > "${META_OUTPUT}"
{
  "trial": ${TRIAL},
  "t_start": "${T_START}",
  "t_fault_injected": "${T_INJECT}",
  "t_fault_recovered": "${T_RECOVER}",
  "t_end": "${T_END}",
  "target_rate_ops": 50,
  "node_failed": "yb2"
}
EOF

    # Verify Data Integrity
    echo "-> Verifying data integrity for Trial ${TRIAL}..."
    uv run scripts/verify_integrity.py --ack-file "${ACK_OUTPUT}" || echo "WARNING: Integrity check reported issues!"

    echo "Trial ${TRIAL} completed successfully. Metrics written to ${CSV_OUTPUT}."
done

echo ""
echo "============================================================"
echo " All ${NUM_TRIALS} trials finished!"
echo " Run 'uv run analysis/analyze_results.py' to generate report."
echo "============================================================"

