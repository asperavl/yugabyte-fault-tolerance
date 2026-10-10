#!/usr/bin/env bash
# ==============================================================================
# scripts/setup.sh: Cluster Lifecycle and Initialization Script
# Spins up the 3-node YugabyteDB cluster, verifies RF3 health, and seeds data.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

echo "============================================================"
echo " [Setup] Starting 3-node YugabyteDB Cluster (RF=3)"
echo "============================================================"

# Ensure .env exists
if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
fi

# 1. Start primary node yb1
echo "-> Starting primary node yb1..."
docker compose up -d yb1

echo -n "Waiting for primary node yb1 to initialize..."
ELAPSED=0
while true; do
    STATUS=$(docker inspect --format='{{json .State.Health.Status}}' yb1 2>/dev/null || echo '"starting"')
    if [ "${STATUS}" = '"healthy"' ]; then
        echo " [HEALTHY (${ELAPSED}s)]"
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    if [ ${ELAPSED} -ge 120 ]; then
        echo ""
        echo "ERROR: Timed out waiting for yb1 to become healthy (status: ${STATUS})."
        docker compose logs --tail=50 yb1
        exit 1
    fi
    echo -n "."
done

# 2. Start follower nodes yb2 and yb3
echo "-> Starting secondary nodes yb2 and yb3..."
docker compose up -d yb2 yb3

for NODE in yb2 yb3; do
    echo -n "Checking ${NODE}..."
    ELAPSED=0
    while true; do
        STATUS=$(docker inspect --format='{{json .State.Health.Status}}' "${NODE}" 2>/dev/null || echo '"starting"')
        if [ "${STATUS}" = '"healthy"' ]; then
            echo " [HEALTHY (${ELAPSED}s)]"
            break
        fi
        sleep 2
        ELAPSED=$((ELAPSED + 2))
        if [ ${ELAPSED} -ge 120 ]; then
            echo ""
            echo "ERROR: Timed out waiting for ${NODE} to become healthy (status: ${STATUS})."
            docker compose logs --tail=50 "${NODE}"
            exit 1
        fi
        echo -n "."
    done
done

# 3. Verify Cluster Membership & Replication
echo "-> Verifying cluster status via yugabyted..."
docker compose exec -T yb1 bin/yugabyted status --base_dir=/home/yugabyte/yb_data || true

# 4. Apply Schema and Seed Data
echo "-> Creating benchmark schema (sql/schema.sql)..."
docker compose exec -T yb1 bin/ysqlsh -h yb1 -U yugabyte -d yugabyte -f - < "${ROOT_DIR}/sql/schema.sql"

echo "-> Seeding 10,000 initial records (sql/seed.sql)..."
docker compose exec -T yb1 bin/ysqlsh -h yb1 -U yugabyte -d yugabyte -f - < "${ROOT_DIR}/sql/seed.sql"

# 5. Validate Seed Count
echo "-> Verifying row count..."
ROW_COUNT=$(docker compose exec -T yb1 bin/ysqlsh -h yb1 -U yugabyte -d yugabyte -t -A -c "SELECT COUNT(*) FROM benchmark_records;")
echo "Total seeded records in benchmark_records: ${ROW_COUNT}"

if [ "${ROW_COUNT}" -ne 10000 ]; then
    echo "ERROR: Expected 10,000 rows, found ${ROW_COUNT}."
    exit 1
fi

echo "============================================================"
echo " [Setup] Cluster initialized and verified successfully!"
echo " YSQL Endpoints: localhost:5433 (yb1), 5434 (yb2), 5435 (yb3)"
echo " Web Dashboards: http://localhost:7001, 7002, 7003"
echo "============================================================"

