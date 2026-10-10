-- Seed 10,000 initial records into benchmark_records
-- Fast, repeatable, idempotent seeding

TRUNCATE TABLE benchmark_records;

INSERT INTO benchmark_records (id, payload, write_counter, client_id, last_updated)
SELECT 
    g,
    'seed_payload_' || md5(g::text),
    0,
    'seeder',
    CURRENT_TIMESTAMP
FROM generate_series(1, 10000) AS g;
