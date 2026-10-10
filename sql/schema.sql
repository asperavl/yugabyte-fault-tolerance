-- Schema definition for YugabyteDB fault-tolerance benchmark
-- Creates benchmark table with stable primary key and metadata tracking

DROP TABLE IF EXISTS benchmark_records CASCADE;

CREATE TABLE benchmark_records (
    id BIGINT PRIMARY KEY,
    payload TEXT NOT NULL,
    write_counter INT NOT NULL DEFAULT 0,
    client_id VARCHAR(64) DEFAULT 'init',
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_benchmark_counter ON benchmark_records(write_counter);
