# YugabyteDB Fault-Tolerance & Reliability Lab

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![YugabyteDB](https://img.shields.io/badge/YugabyteDB-2024.1.3.0--b105-orange.svg)](https://hub.docker.com/r/yugabytedb/yugabyte)
[![Python](https://img.shields.io/badge/Python-3.12-green.svg)](https://www.python.org/)
[![Package Manager](https://img.shields.io/badge/Package%20Manager-uv-purple.svg)](https://astral.sh/uv)

A locally reproducible distributed systems reliability experiment evaluating **YugabyteDB (YSQL)** behavior under node failures. This project simulates continuous production traffic (~50 operations/sec, 70% reads / 30% writes), deliberately injects node outages into a 3-node cluster ($RF=3$), and empirically measures client-perceived failover latency, Raft leader re-election, recovery time, and data durability ($RPO=0$).

---

## Architecture Overview

```
                      +---------------------------------------+
                      |       loadgen.py (Client Traffic)      |
                      |  - 50 ops/sec (70% Read / 30% Write)  |
                      |  - Multi-host failover connection     |
                      |  - Bounded retries (backoff & ledger) |
                      +-------------------+-------------------+
                                          |
                      +-------------------+-------------------+
                      |      Docker Bridge Network: yb_net    |
                      +-------+---------------+---------------+
                              |               |               |
                Port 5433     | Port 5434     | Port 5435     |
             +----------------+ +-------------+--+ +----------+------+
             |   Node: yb1    | |   Node: yb2    | |   Node: yb3    |
             |  (Master+TS)   | |  (Master+TS)   | |  (Master+TS)   |
             |  Status: ALIVE | | Status: KILLED | |  Status: ALIVE |
             +-------+--------+ +-------+--------+ +-------+--------+
                     |                  |                  |
           [Volume: yb1_data] [Volume: yb2_data] [Volume: yb3_data]
                     \                  |                  /
                      +-----------------+-----------------+
                      |    Raft Consensus Group (RF=3)    |
                      |  Quorum = 2/3 (Cluster Operates)  |
                      +-----------------------------------+
```

### Key Components
- **Cluster**: 3 YugabyteDB container nodes (`yb1`, `yb2`, `yb3`) running combined Master and Tablet Server roles via `yugabyted`.
- **Replication**: Replication Factor 3 ($RF=3$). The cluster tolerates any single node failure without data loss or service unavailability.
- **Client Multi-Host Connection**: PostgreSQL/YSQL client connection strings specify all three nodes with `target_session_attrs=any` and connection timeouts.
- **Failover Mechanics**: When `yb2` is stopped, Raft leader leases for tablets hosted on `yb2` expire in ~3.5 seconds. Remaining nodes (`yb1`, `yb3`) form a majority quorum (2 of 3) and elect new tablet leaders. Client requests experience a brief retry window before seamlessly resuming.

---

## Directory Structure

```text
yugabyte-fault-tolerance/
├── docker-compose.yml       # 3-node RF=3 cluster configuration with health checks
├── .env.example             # Pinned image, port mappings, and experiment settings
├── .env                     # Local environment settings
├── .gitignore               # Ignored venv, cache, and large outputs
├── pyproject.toml           # Python 3.12 dependencies managed via uv
├── loadgen.py               # 50 ops/sec workload generator with retry & latency logging
├── scripts/
│   ├── setup.sh             # 1-click startup, health wait, schema, and 10,000-row seed
│   ├── failover_test.sh     # Automates the 180s cycle (baseline -> stop yb2 -> restart yb2)
│   └── verify_integrity.py  # Audits acknowledged writes against the cluster state
├── sql/
│   ├── schema.sql           # Benchmark table definition with indexes
│   └── seed.sql             # Deterministic 10,000 row generator using generate_series
├── results/
│   ├── trial-1.csv          # Representative recorded experimental latency data
│   ├── trial-1_meta.json    # Exact microsecond timestamps of injection/recovery
│   └── ack_writes_trial_1.json # Client write-ledger for trial 1
├── analysis/
│   └── analyze_results.py   # Statistical engine (p50/p95/p99 by phase, recovery time)
├── README.md                # System documentation and operational runbook
└── RESULTS.md               # Experimental analysis, benchmark metrics, and findings
```

---

## Prerequisites & Environment Requirements

- **Operating System**: Linux or **WSL2** (Ubuntu 22.04 or later recommended).
- **Container Runtime**: Docker Engine or Docker Desktop with WSL2 integration enabled.
- **Hardware Resources**:
  - Minimum 4 CPU cores.
  - Minimum 6 GB RAM allocated to WSL2/Docker (configured in `~/.wslconfig`).
- **Python**: Python 3.12+ with [`uv`](https://github.com/astral-sh/uv) installed.

### Installing `uv` (if not already installed)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env
```

---

## Quickstart Guide

### 1. Initialize and Start the Cluster
Run the automated setup script. This script boots the 3 containers, waits for all health checks to pass, and seeds 10,000 initial records into YSQL:

```bash
chmod +x scripts/*.sh
./scripts/setup.sh
```

### 2. Verify Cluster Health & Web Dashboards
Access the running nodes via your browser or terminal:
- **`yb1` Admin Console**: [http://localhost:7001](http://localhost:7001)
- **`yb2` Admin Console**: [http://localhost:7002](http://localhost:7002)
- **`yb3` Admin Console**: [http://localhost:7003](http://localhost:7003)

Verify cluster status directly from the container:
```bash
docker compose exec yb1 bin/yugabyted status --base_dir=/home/yugabyte/yb_data
```

---

## Running the Fault-Injection Experiment

The test harness automates the complete 180-second experimental timeline:
1. **0s – 60s (Baseline)**: Steady-state 50 ops/sec traffic to establish baseline p50/p95/p99 latency.
2. **60s (Fault Injection)**: Stops container `yb2` using `docker compose stop yb2`.
3. **60s – 120s (Outage Window)**: Cluster operates in degraded mode ($N=2$, Quorum=2/3). Measures client reconnection and tablet leader transition.
4. **120s (Recovery)**: Restarts `yb2` using `docker compose start yb2`.
5. **120s – 180s (Stabilization)**: Node rejoins Raft cluster, catches up WAL logs, and traffic stabilizes.
6. **Data Integrity Audit**: Automatically executes `scripts/verify_integrity.py` to ensure zero lost writes ($RPO=0$).

### Execute a Single Trial
```bash
./scripts/failover_test.sh 1
```

### Execute the Full 5-Trial Suite
```bash
./scripts/failover_test.sh 5
```

---

## Data Analysis & Reporting

To compute latency percentiles across the experiment phases (Baseline vs Outage vs Recovery) and format summary tables:

```bash
uv run analysis/analyze_results.py
```

This generates:
- Total operations, direct successes, recovered retry successes, and failures.
- Percentile breakdown ($p50$, $p95$, $p99$) segmented by phase.
- Client-visible recovery time ($T_{recovery}$).
- Durable write audit confirmation.

---

## Manual Verification & Inspection

### Run Workload Standalone
```bash
uv run loadgen.py --rate 50 --read-ratio 0.7 --duration 60 --output results/manual_test.csv
```

### Run Integrity Audit Standalone
```bash
uv run scripts/verify_integrity.py --ack-file results/ack_writes_trial_1.json
```

### Inspect Database Directly via YSQL Shell
```bash
docker compose exec -it yb1 bin/ysqlsh -U yugabyte -d yugabyte
```
```sql
SELECT COUNT(*) FROM benchmark_records;
SELECT id, payload, write_counter, last_updated FROM benchmark_records ORDER BY write_counter DESC LIMIT 5;
```

---

## Troubleshooting & FAQ

| Problem | Cause | Solution |
| :--- | :--- | :--- |
| **`bind: address already in use`** | PostgreSQL or previous Yugabyte instance is using host ports `5433-5435` or `7001-7003`. | Run `docker compose down -v` or change the port mappings in `.env`. |
| **Containers killed by OOM** | Docker/WSL2 ran out of memory. | Add `memory=6GB` to `~/.wslconfig` and restart WSL using `wsl --shutdown`. |
| **Node 2 fails to join Node 1** | Node 1 was not fully healthy when Node 2 started. | `docker-compose.yml` uses `depends_on: {yb1: {condition: service_healthy}}`. If starting manually, ensure `yb1` web UI is up first. |
| **Script permissions issue in WSL** | Windows line endings (`CRLF`) or missing executable bit. | Run `dos2unix scripts/*.sh && chmod +x scripts/*.sh`. |

---

## Teardown & Reset

To cleanly stop the cluster and retain data:
```bash
docker compose down
```

To purge all containers, networks, and persistent data volumes for a clean slate:
```bash
docker compose down -v
```

