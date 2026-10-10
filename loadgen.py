#!/usr/bin/env python3
"""
Workload Generator for YugabyteDB Fault-Tolerance Lab (loadgen.py)

Generates continuous synthetic database traffic (~50 ops/sec, 70% reads, 30% writes),
handles bounded retries, supports client multi-node failover, and logs every operation
with microsecond timestamps, latency, outcome, and retry count.
"""

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("loadgen")


class WorkloadGenerator:
    def __init__(
        self,
        conn_info: str,
        rate: float = 50.0,
        read_ratio: float = 0.7,
        duration: int = 180,
        max_retries: int = 3,
        timeout: float = 2.0,
        output_csv: str = "results/trial-1.csv",
        ack_output: str = "results/ack_writes.json",
        table_size: int = 10000,
    ):
        self.conn_info = conn_info
        self.rate = rate
        self.read_ratio = read_ratio
        self.duration = duration
        self.max_retries = max_retries
        self.timeout = timeout
        self.output_csv = output_csv
        self.ack_output = ack_output
        self.table_size = table_size

        self.conn = None
        self.acknowledged_writes = {}  # {key_id: {"write_counter": val, "payload": str, "timestamp": str}}
        self.csv_file = None
        self.csv_writer = None

    def connect(self) -> psycopg.Connection:
        """Establishes or re-establishes a database connection."""
        if self.conn and not self.conn.closed:
            try:
                self.conn.close()
            except Exception:
                pass

        logger.debug(f"Connecting to database with conninfo: {self.conn_info}")
        conn = psycopg.connect(
            self.conn_info,
            autocommit=True,
            connect_timeout=int(self.timeout),
        )
        return conn

    def get_connection(self) -> psycopg.Connection:
        """Returns active connection or reconnects if broken."""
        if self.conn is None or self.conn.closed:
            self.conn = self.connect()
        return self.conn

    def execute_read(self, key_id: int) -> dict:
        """Executes a parameterized SELECT query."""
        conn = self.get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, payload, write_counter FROM benchmark_records WHERE id = %s;",
                (key_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Key {key_id} not found in database")
            return {"id": row[0], "write_counter": row[2]}

    def execute_write(self, key_id: int, client_id: str) -> dict:
        """Executes a parameterized UPDATE query incrementing write_counter."""
        conn = self.get_connection()
        new_payload = f"update_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE benchmark_records
                SET write_counter = write_counter + 1,
                    payload = %s,
                    client_id = %s,
                    last_updated = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING write_counter, payload;
                """,
                (new_payload, client_id, key_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Key {key_id} update affected 0 rows")
            return {"key_id": key_id, "write_counter": row[0], "payload": row[1]}

    def run(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.output_csv)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.ack_output)), exist_ok=True)

        fieldnames = [
            "op_id",
            "timestamp",
            "op_type",
            "key_id",
            "latency_ms",
            "status",
            "retries",
            "error_type",
            "error_msg",
        ]

        self.csv_file = open(self.output_csv, mode="w", newline="", encoding="utf-8")
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=fieldnames)
        self.csv_writer.writeheader()

        client_id = f"client_{os.getpid()}"
        start_time = time.time()
        end_time = start_time + self.duration
        target_interval = 1.0 / self.rate

        logger.info(
            f"Starting workload: rate={self.rate} ops/s, read_ratio={self.read_ratio}, "
            f"duration={self.duration}s, max_retries={self.max_retries}"
        )

        op_id = 0
        total_success = 0
        total_retried_success = 0
        total_failed = 0

        next_tick = time.monotonic()

        try:
            while time.time() < end_time:
                op_id += 1
                op_type = "READ" if random.random() < self.read_ratio else "WRITE"
                key_id = random.randint(1, self.table_size)

                op_start_wall = datetime.now(timezone.utc).isoformat()
                op_start_mono = time.monotonic()
                retries = 0
                outcome = "FAILURE"
                error_type = ""
                error_msg = ""
                write_meta = None

                while retries <= self.max_retries:
                    try:
                        if op_type == "READ":
                            self.execute_read(key_id)
                        else:
                            write_meta = self.execute_write(key_id, client_id)

                        outcome = "SUCCESS" if retries == 0 else "RETRY_SUCCESS"
                        error_type = ""
                        error_msg = ""
                        break
                    except Exception as ex:
                        retries += 1
                        error_type = type(ex).__name__
                        error_msg = str(ex).replace("\n", " ").strip()[:150]
                        logger.debug(
                            f"Op {op_id} ({op_type}) attempt {retries} failed: {error_type}: {error_msg}"
                        )
                        # Invalidate active connection so retry forces reconnect/failover
                        if self.conn:
                            try:
                                self.conn.close()
                            except Exception:
                                pass
                            self.conn = None

                        if retries <= self.max_retries:
                            backoff = min(0.5, 0.05 * (2 ** (retries - 1)))
                            time.sleep(backoff)

                op_end_mono = time.monotonic()
                latency_ms = (op_end_mono - op_start_mono) * 1000.0

                if outcome == "SUCCESS":
                    total_success += 1
                    if op_type == "WRITE" and write_meta:
                        self.acknowledged_writes[key_id] = {
                            "write_counter": write_meta["write_counter"],
                            "payload": write_meta["payload"],
                            "timestamp": op_start_wall,
                        }
                elif outcome == "RETRY_SUCCESS":
                    total_retried_success += 1
                    if op_type == "WRITE" and write_meta:
                        self.acknowledged_writes[key_id] = {
                            "write_counter": write_meta["write_counter"],
                            "payload": write_meta["payload"],
                            "timestamp": op_start_wall,
                        }
                else:
                    total_failed += 1

                self.csv_writer.writerow(
                    {
                        "op_id": op_id,
                        "timestamp": op_start_wall,
                        "op_type": op_type,
                        "key_id": key_id,
                        "latency_ms": f"{latency_ms:.3f}",
                        "status": outcome,
                        "retries": min(retries, self.max_retries),
                        "error_type": error_type,
                        "error_msg": error_msg,
                    }
                )

                if op_id % 200 == 0:
                    elapsed = time.time() - start_time
                    logger.info(
                        f"Progress: {elapsed:.1f}s / {self.duration}s | Ops: {op_id} | "
                        f"Success: {total_success} | Recovered: {total_retried_success} | Failed: {total_failed}"
                    )

                next_tick += target_interval
                sleep_duration = next_tick - time.monotonic()
                if sleep_duration > 0:
                    time.sleep(sleep_duration)
                else:
                    # Fallen behind; reset next_tick to prevent burst flooding
                    next_tick = time.monotonic()

        finally:
            if self.csv_file:
                self.csv_file.flush()
                self.csv_file.close()

            if self.conn and not self.conn.closed:
                try:
                    self.conn.close()
                except Exception:
                    pass

            # Save acknowledged writes for verify_integrity.py
            with open(self.ack_output, "w", encoding="utf-8") as f:
                json.dump(self.acknowledged_writes, f, indent=2)

            logger.info(
                f"Workload completed. Total logical ops: {op_id}. "
                f"Direct success: {total_success}, Recovered on retry: {total_retried_success}, "
                f"Permanent failures: {total_failed}. "
                f"Acknowledged unique write keys: {len(self.acknowledged_writes)}."
            )
            logger.info(f"Results written to: {self.output_csv}")
            logger.info(f"Acknowledged writes written to: {self.ack_output}")


def main():
    parser = argparse.ArgumentParser(description="YugabyteDB Workload Generator")
    parser.add_argument(
        "--hosts",
        type=str,
        default="localhost:5433,localhost:5434,localhost:5435",
        help="Comma-separated host:port list for multi-host failover connection",
    )
    parser.add_argument("--user", type=str, default="yugabyte", help="Database user")
    parser.add_argument("--password", type=str, default="", help="Database password")
    parser.add_argument("--dbname", type=str, default="yugabyte", help="Database name")
    parser.add_argument("--rate", type=float, default=50.0, help="Target operations/sec")
    parser.add_argument("--read-ratio", type=float, default=0.7, help="Fraction of reads (0.0 - 1.0)")
    parser.add_argument("--duration", type=int, default=180, help="Duration in seconds")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retry attempts per operation")
    parser.add_argument("--timeout", type=float, default=2.0, help="Socket/connect timeout in seconds")
    parser.add_argument("--output", type=str, default="results/trial-1.csv", help="Output CSV path")
    parser.add_argument("--ack-output", type=str, default="results/ack_writes.json", help="Ack writes output path")
    parser.add_argument("--table-size", type=int, default=10000, help="Key space range (default 1-10000)")

    args = parser.parse_args()

    # Build multi-host connection string for psycopg
    # psycopg multi-host format: host=h1,h2 port=p1,p2 or host=h1:p1,h2:p2
    host_list = [h.strip() for h in args.hosts.split(",")]
    hosts = []
    ports = []
    for item in host_list:
        if ":" in item:
            h, p = item.split(":")
            hosts.append(h)
            ports.append(p)
        else:
            hosts.append(item)
            ports.append("5433")

    conn_str = (
        f"host={','.join(hosts)} port={','.join(ports)} "
        f"dbname={args.dbname} user={args.user} "
        f"target_session_attrs=any "
        f"options='-c statement_timeout=3000'"
    )
    if args.password:
        conn_str += f" password={args.password}"

    gen = WorkloadGenerator(
        conn_info=conn_str,
        rate=args.rate,
        read_ratio=args.read_ratio,
        duration=args.duration,
        max_retries=args.max_retries,
        timeout=args.timeout,
        output_csv=args.output,
        ack_output=args.ack_output,
        table_size=args.table_size,
    )
    gen.run()


if __name__ == "__main__":
    main()

