-- Run in Navicat after selecting the chainscope database.
SELECT VERSION() AS mysql_version;
SELECT version_num FROM alembic_version;

SELECT batch_id, token_address, start_block, end_block, status,
       event_count, inserted_count, existing_count, receipt_verified_events,
       error_type, started_at_utc, finished_at_utc, raw_directory
FROM collection_batches
ORDER BY started_at_utc DESC;

SELECT chain_id, tx_hash, log_index, COUNT(*) AS copies
FROM transfer_events
GROUP BY chain_id, tx_hash, log_index
HAVING COUNT(*) > 1;
-- Expected: no rows, enforced by uq_transfer_identity.

SELECT b.batch_id, b.status, b.event_count, COUNT(be.event_id) AS linked_events
FROM collection_batches AS b
LEFT JOIN batch_events AS be ON be.batch_id = b.batch_id
GROUP BY b.batch_id, b.status, b.event_count;

SELECT block_number, tx_hash, log_index, from_address, to_address,
       amount_raw, token_decimals, block_time_utc
FROM transfer_events
WHERE chain_id = 56
  AND token_address = '0xd270d4e1ec6e6e0d28c0ecb8be966ec75997ffff'
  AND block_number BETWEEN 120607788 AND 120607807
ORDER BY block_number, log_index;
-- amount_raw is a decimal string preserving all 78 uint256 digits.
-- Do not SUM(amount_raw), which coerces strings; use the Python verify command.
