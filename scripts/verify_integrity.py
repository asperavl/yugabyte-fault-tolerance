#!/usr/bin/env python3
"""
scripts/verify_integrity.py: Data Integrity and Consistency Verifier

Cross-checks client-acknowledged write operations against actual database state
after node failure and recovery. Verifies that zero committed writes were lost (RPO=0).
"""

import argparse
import json
import logging
import os
import sys
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("integrity-verifier")


def verify_integrity(ack_file: str, conn_str: str) -> bool:
    if not os.path.exists(ack_file):
        logger.error(f"Acknowledged writes file not found: {ack_file}")
        return False

    with open(ack_file, "r", encoding="utf-8") as f:
        ack_data = json.load(f)

    total_ack_keys = len(ack_data)
    if total_ack_keys == 0:
        logger.warning("No acknowledged writes found to verify.")
        return True

    logger.info(f"Verifying {total_ack_keys} acknowledged write keys against cluster...")

    missing_keys = []
    stale_counters = []
    verified_keys = 0

    try:
        with psycopg.connect(conn_str, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                # Query in batches of 500 keys
                key_list = [int(k) for k in ack_data.keys()]
                batch_size = 500

                for i in range(0, len(key_list), batch_size):
                    batch = key_list[i : i + batch_size]
                    cur.execute(
                        """
                        SELECT id, write_counter, payload
                        FROM benchmark_records
                        WHERE id = ANY(%s);
                        """,
                        (batch,),
                    )
                    db_rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

                    for key in batch:
                        if key not in db_rows:
                            missing_keys.append(key)
                            continue

                        db_counter, db_payload = db_rows[key]
                        ack_counter = ack_data[str(key)]["write_counter"]
                        ack_payload = ack_data[str(key)]["payload"]

                        # In concurrent operations on the same key, db_counter >= ack_counter
                        if db_counter < ack_counter:
                            stale_counters.append(
                                {
                                    "key": key,
                                    "expected_counter": ack_counter,
                                    "actual_counter": db_counter,
                                }
                            )
                        else:
                            verified_keys += 1

    except Exception as ex:
        logger.error(f"Failed to query database during integrity verification: {ex}")
        return False

    logger.info("---------------- DATA INTEGRITY REPORT ----------------")
    logger.info(f"Total Acknowledged Write Keys: {total_ack_keys}")
    logger.info(f"Successfully Verified Keys:   {verified_keys}")
    logger.info(f"Missing Keys (Data Loss):      {len(missing_keys)}")
    logger.info(f"Stale/Rolled Back Counters:    {len(stale_counters)}")
    logger.info("-------------------------------------------------------")

    if missing_keys:
        logger.error(f"Missing keys sample: {missing_keys[:10]}")
    if stale_counters:
        logger.error(f"Stale counters sample: {stale_counters[:10]}")

    if not missing_keys and not stale_counters:
        logger.info("RESULT: PASS - All acknowledged writes exist and are durable (RPO = 0).")
        return True
    else:
        logger.error("RESULT: FAIL - Detected data inconsistencies or lost writes!")
        return False


def main():
    parser = argparse.ArgumentParser(description="Verify YugabyteDB Data Integrity")
    parser.add_argument(
        "--ack-file",
        type=str,
        default="results/ack_writes.json",
        help="Path to JSON file containing acknowledged writes",
    )
    parser.add_argument(
        "--hosts",
        type=str,
        default="localhost:5433,localhost:5434,localhost:5435",
        help="Cluster host list",
    )
    parser.add_argument("--dbname", type=str, default="yugabyte")
    parser.add_argument("--user", type=str, default="yugabyte")
    parser.add_argument("--password", type=str, default="")

    args = parser.parse_args()

    host_list = [h.strip() for h in args.hosts.split(",")]
    hosts, ports = [], []
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
        f"target_session_attrs=any"
    )
    if args.password:
        conn_str += f" password={args.password}"

    success = verify_integrity(args.ack_file, conn_str)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

