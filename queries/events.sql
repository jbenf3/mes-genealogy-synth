-- The event table: every row of one output folder, base load and stream (columns in docs/SCHEMA.md).
-- Set the folder first, for example: SET VARIABLE data = 'out/official_1x';
CREATE OR REPLACE TEMP VIEW events AS
SELECT * FROM read_parquet([getvariable('data') || '/base/*.parquet', getvariable('data') || '/stream/*/*.parquet'],
                           hive_partitioning = false);
