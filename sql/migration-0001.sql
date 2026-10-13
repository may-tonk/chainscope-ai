CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE TABLE collection_batches (
    batch_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    chain_id INTEGER UNSIGNED NOT NULL, 
    token_address CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    start_block BIGINT UNSIGNED NOT NULL, 
    end_block BIGINT UNSIGNED NOT NULL, 
    end_block_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin, 
    provider VARCHAR(32) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    started_at_utc DATETIME(6) NOT NULL, 
    finished_at_utc DATETIME(6), 
    raw_directory VARCHAR(512) NOT NULL, 
    event_count INTEGER UNSIGNED NOT NULL DEFAULT '0', 
    inserted_count INTEGER UNSIGNED NOT NULL DEFAULT '0', 
    existing_count INTEGER UNSIGNED NOT NULL DEFAULT '0', 
    receipt_verified_events INTEGER UNSIGNED NOT NULL DEFAULT '0', 
    error_type VARCHAR(64), 
    PRIMARY KEY (batch_id), 
    CONSTRAINT ck_batch_status CHECK (status IN ('running', 'succeeded', 'failed')), 
    CONSTRAINT ck_batch_range CHECK (end_block >= start_block)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE TABLE transfer_events (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, 
    chain_id INTEGER UNSIGNED NOT NULL, 
    token_address CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    block_number BIGINT UNSIGNED NOT NULL, 
    block_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    block_time_utc DATETIME(6) NOT NULL, 
    tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    log_index INTEGER UNSIGNED NOT NULL, 
    from_address CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    to_address CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    amount_raw VARCHAR(78) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    token_decimals TINYINT UNSIGNED, 
    PRIMARY KEY (event_id), 
    CONSTRAINT uq_transfer_identity UNIQUE (chain_id, tx_hash, log_index), 
    CONSTRAINT ck_amount_digits CHECK (amount_raw REGEXP '^(0|[1-9][0-9]{0,77})$'), 
    CONSTRAINT ck_amount_uint256 CHECK (CHAR_LENGTH(amount_raw) < 78 OR amount_raw <= '115792089237316195423570985008687907853269984665640564039457584007913129639935')
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_transfer_token_block ON transfer_events (chain_id, token_address, block_number);

CREATE INDEX ix_transfer_to_block ON transfer_events (chain_id, to_address, block_number);

CREATE INDEX ix_transfer_from_block ON transfer_events (chain_id, from_address, block_number);

CREATE TABLE batch_events (
    batch_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    event_id BIGINT UNSIGNED NOT NULL, 
    PRIMARY KEY (batch_id, event_id), 
    FOREIGN KEY(batch_id) REFERENCES collection_batches (batch_id), 
    FOREIGN KEY(event_id) REFERENCES transfer_events (event_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

INSERT INTO alembic_version (version_num) VALUES ('0001');

