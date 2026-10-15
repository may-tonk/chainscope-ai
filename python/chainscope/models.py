"""SQLAlchemy Core schema. Initial DDL is frozen separately in Alembic."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

metadata = sa.MetaData()
ascii42 = mysql.CHAR(42, charset="ascii", collation="ascii_bin")
ascii66 = mysql.CHAR(66, charset="ascii", collation="ascii_bin")
batch_id_type = mysql.CHAR(36, charset="ascii", collation="ascii_bin")
job_id_type = mysql.VARCHAR(32, charset="ascii", collation="ascii_bin")
options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

batches = sa.Table(
    "collection_batches",
    metadata,
    sa.Column("batch_id", batch_id_type, primary_key=True),
    sa.Column("chain_id", mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column("token_address", ascii42, nullable=False),
    sa.Column("start_block", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("end_block", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("end_block_hash", ascii66),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("started_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    sa.Column("finished_at_utc", mysql.DATETIME(fsp=6)),
    sa.Column("raw_directory", sa.String(512), nullable=True),
    sa.Column(
        "event_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"
    ),
    sa.Column(
        "inserted_count",
        mysql.INTEGER(unsigned=True),
        nullable=False,
        server_default="0",
    ),
    sa.Column(
        "existing_count",
        mysql.INTEGER(unsigned=True),
        nullable=False,
        server_default="0",
    ),
    sa.Column(
        "receipt_verified_events",
        mysql.INTEGER(unsigned=True),
        nullable=False,
        server_default="0",
    ),
    sa.Column("error_type", sa.String(64)),
    # Set when the attempt processed a coverage window of a scan job.
    sa.Column("job_id", job_id_type, nullable=True),
    sa.CheckConstraint(
        "status IN ('running', 'succeeded', 'failed')", name="ck_batch_status"
    ),
    sa.CheckConstraint("end_block >= start_block", name="ck_batch_range"),
    **options,
)

transfers = sa.Table(
    "transfer_events",
    metadata,
    sa.Column(
        "event_id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True
    ),
    sa.Column("chain_id", mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column("token_address", ascii42, nullable=False),
    sa.Column("block_number", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("block_hash", ascii66, nullable=False),
    sa.Column("block_time_utc", mysql.DATETIME(fsp=6), nullable=False),
    sa.Column("tx_hash", ascii66, nullable=False),
    sa.Column("log_index", mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column("from_address", ascii42, nullable=False),
    sa.Column("to_address", ascii42, nullable=False),
    sa.Column(
        "amount_raw",
        mysql.VARCHAR(78, charset="ascii", collation="ascii_bin"),
        nullable=False,
    ),
    sa.Column("token_decimals", mysql.TINYINT(unsigned=True)),
    # Digit count, so (amount_len, amount_raw) sorts amounts numerically.
    sa.Column(
        "amount_len",
        mysql.TINYINT(unsigned=True),
        sa.Computed("CHAR_LENGTH(amount_raw)", persisted=True),
    ),
    sa.UniqueConstraint(
        "chain_id", "tx_hash", "log_index", name="uq_transfer_identity"
    ),
    sa.CheckConstraint(
        "amount_raw REGEXP '^(0|[1-9][0-9]{0,77})$'", name="ck_amount_digits"
    ),
    sa.CheckConstraint(
        "CHAR_LENGTH(amount_raw) < 78 OR amount_raw <= '115792089237316195423570985008687907853269984665640564039457584007913129639935'",
        name="ck_amount_uint256",
    ),
    sa.Index("ix_transfer_token_block", "chain_id", "token_address", "block_number"),
    sa.Index("ix_transfer_from_block", "chain_id", "from_address", "block_number"),
    sa.Index("ix_transfer_to_block", "chain_id", "to_address", "block_number"),
    sa.Index(
        "ix_transfer_amount", "chain_id", "token_address", "amount_len", "amount_raw"
    ),
    **options,
)

batch_events = sa.Table(
    "batch_events",
    metadata,
    sa.Column(
        "batch_id",
        batch_id_type,
        sa.ForeignKey("collection_batches.batch_id"),
        primary_key=True,
    ),
    sa.Column(
        "event_id",
        mysql.BIGINT(unsigned=True),
        sa.ForeignKey("transfer_events.event_id"),
        primary_key=True,
    ),
    **options,
)

jobs = sa.Table(
    "scan_jobs",
    metadata,
    sa.Column("job_id", job_id_type, primary_key=True),
    sa.Column("chain_id", mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column("token_address", ascii42, nullable=False),
    sa.Column("token_decimals", mysql.TINYINT(unsigned=True)),
    sa.Column("source", sa.String(16), nullable=False),
    sa.Column("start_block", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("window_size", mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column("parser_version", mysql.SMALLINT(unsigned=True), nullable=False),
    # Highest block such that every window from start_block up to it is done.
    sa.Column("verified_through", mysql.BIGINT(unsigned=True)),
    # address_balances holds every transfer up to this block; NULL means rebuild.
    sa.Column("balances_through", mysql.BIGINT(unsigned=True)),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    sa.UniqueConstraint("chain_id", "token_address", "source", name="uq_job_target"),
    sa.CheckConstraint("status IN ('active', 'paused')", name="ck_job_status"),
    sa.CheckConstraint("window_size > 0", name="ck_job_window_size"),
    **options,
)

windows = sa.Table(
    "coverage_windows",
    metadata,
    sa.Column(
        "job_id", job_id_type, sa.ForeignKey("scan_jobs.job_id"), primary_key=True
    ),
    sa.Column("start_block", mysql.BIGINT(unsigned=True), primary_key=True),
    sa.Column("end_block", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("attempt_id", batch_id_type),
    sa.Column(
        "attempts", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"
    ),
    sa.Column("event_count", mysql.INTEGER(unsigned=True)),
    sa.Column("inserted_count", mysql.INTEGER(unsigned=True)),
    sa.Column("amount_sum", mysql.VARCHAR(90, charset="ascii", collation="ascii_bin")),
    sa.Column("fingerprint", mysql.BINARY(32)),
    sa.Column("source_head", mysql.BIGINT(unsigned=True)),
    sa.Column("parser_version", mysql.SMALLINT(unsigned=True)),
    sa.Column("reconcile", sa.String(16)),
    sa.Column("receipt_checked", mysql.INTEGER(unsigned=True)),
    sa.Column("swap_count", mysql.INTEGER(unsigned=True)),
    sa.Column("error_type", sa.String(64)),
    sa.Column("updated_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    sa.CheckConstraint(
        "status IN ('pending', 'running', 'done', 'failed')", name="ck_window_status"
    ),
    sa.CheckConstraint("end_block >= start_block", name="ck_window_range"),
    sa.Index("ix_window_status", "job_id", "status", "start_block"),
    **options,
)

window_events = sa.Table(
    "window_events",
    metadata,
    sa.Column("job_id", job_id_type, primary_key=True),
    sa.Column("start_block", mysql.BIGINT(unsigned=True), primary_key=True),
    sa.Column(
        "event_id",
        mysql.BIGINT(unsigned=True),
        sa.ForeignKey("transfer_events.event_id"),
        primary_key=True,
    ),
    sa.ForeignKeyConstraint(
        ["job_id", "start_block"],
        ["coverage_windows.job_id", "coverage_windows.start_block"],
        name="fk_window_events_window",
    ),
    **options,
)

window_diffs = sa.Table(
    "window_diffs",
    metadata,
    sa.Column(
        "diff_id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True
    ),
    sa.Column("job_id", job_id_type, nullable=False),
    sa.Column("start_block", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("end_block", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("found_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    # missing_in_alchemy / missing_in_sqd / field_conflict / recheck_changed
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("tx_hash", ascii66),
    sa.Column("log_index", mysql.INTEGER(unsigned=True)),
    sa.Column("detail", sa.JSON, nullable=False),
    sa.Index("ix_diff_window", "job_id", "start_block"),
    **options,
)

digits78 = mysql.VARCHAR(78, charset="ascii", collation="ascii_bin")
balance_type = mysql.DECIMAL(65, 0)
price_type = mysql.DECIMAL(50, 30)
usd_type = mysql.DECIMAL(38, 2)

pools = sa.Table(
    "dex_pools",
    metadata,
    sa.Column("job_id", job_id_type, primary_key=True),
    sa.Column("pool_address", ascii42, primary_key=True),
    sa.Column("dex", sa.String(32), nullable=False),
    sa.Column("token0", ascii42, nullable=False),
    sa.Column("token1", ascii42, nullable=False),
    sa.Column("token_is_token0", sa.Boolean, nullable=False),
    sa.Column("quote_token", ascii42, nullable=False),
    sa.Column("quote_symbol", sa.String(16)),
    sa.Column("fee", mysql.INTEGER(unsigned=True)),
    sa.Column("created_block", mysql.BIGINT(unsigned=True), nullable=False),
    # Only enabled pools are collected; unknown quote tokens start disabled.
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("found_by", sa.String(16), nullable=False),
    sa.Column("added_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    **options,
)

swaps = sa.Table(
    "swap_events",
    metadata,
    sa.Column(
        "swap_id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True
    ),
    sa.Column("job_id", job_id_type, nullable=False),
    sa.Column("chain_id", mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column("pool_address", ascii42, nullable=False),
    sa.Column("block_number", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("block_time_utc", mysql.DATETIME(fsp=6), nullable=False),
    sa.Column("tx_hash", ascii66, nullable=False),
    sa.Column("log_index", mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column("tx_from", ascii42, nullable=False),
    sa.Column("trader", ascii42, nullable=False),
    sa.Column("side", sa.String(4), nullable=False),
    sa.Column("token_amount_raw", digits78, nullable=False),
    sa.Column("quote_amount_raw", digits78, nullable=False),
    sa.Column("quote_token", ascii42, nullable=False),
    # direct / router / ambiguous: how surely the trader is the tx sender.
    sa.Column("attribution", sa.String(16), nullable=False),
    sa.Column("transfer_matched", sa.Boolean, nullable=False),
    sa.UniqueConstraint("job_id", "tx_hash", "log_index", name="uq_swap_identity"),
    sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_swap_side"),
    sa.Index("ix_swap_block", "job_id", "block_number"),
    sa.Index("ix_swap_trader", "job_id", "trader", "block_number"),
    sa.Index("ix_swap_pool", "pool_address", "block_number"),
    **options,
)

balances = sa.Table(
    "address_balances",
    metadata,
    sa.Column("chain_id", mysql.INTEGER(unsigned=True), primary_key=True),
    sa.Column("token_address", ascii42, primary_key=True),
    sa.Column("address", ascii42, primary_key=True),
    sa.Column("balance_raw", balance_type, nullable=False),
    sa.Column("last_block", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Index("ix_balance_rank", "chain_id", "token_address", "balance_raw"),
    **options,
)

address_info = sa.Table(
    "address_info",
    metadata,
    sa.Column("chain_id", mysql.INTEGER(unsigned=True), primary_key=True),
    sa.Column("address", ascii42, primary_key=True),
    sa.Column("is_contract", sa.Boolean, nullable=False),
    sa.Column("code_size", mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column("checked_block", mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column("checked_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    **options,
)

labels = sa.Table(
    "address_labels",
    metadata,
    sa.Column("chain_id", mysql.INTEGER(unsigned=True), primary_key=True),
    sa.Column("address", ascii42, primary_key=True),
    sa.Column("label", sa.String(64), nullable=False),
    # cex / dex / team / burn / contract / other
    sa.Column("category", sa.String(16), nullable=False),
    sa.Column("source", sa.String(200), nullable=False),
    # Third-party labels stay unverified until someone confirms them.
    sa.Column("verified", sa.Boolean, nullable=False),
    sa.Column("updated_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    **options,
)

market_snapshots = sa.Table(
    "market_snapshots",
    metadata,
    sa.Column(
        "snapshot_id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True
    ),
    sa.Column("token_address", ascii42, nullable=False),
    sa.Column("source", sa.String(16), nullable=False),
    sa.Column("fetched_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    sa.Column("listed", sa.Boolean, nullable=False),
    sa.Column("price_usd", price_type),
    sa.Column("market_cap_usd", usd_type),
    sa.Column("volume_24h_usd", usd_type),
    sa.Column("source_updated_at_utc", mysql.DATETIME(fsp=6)),
    sa.Index("ix_market_time", "token_address", "fetched_at_utc"),
    **options,
)

pool_snapshots = sa.Table(
    "pool_snapshots",
    metadata,
    sa.Column(
        "snapshot_id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True
    ),
    sa.Column("pool_address", ascii42, nullable=False),
    sa.Column("fetched_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    sa.Column("price_usd", price_type),
    sa.Column("price_quote", price_type),
    sa.Column("reserve_usd", usd_type),
    sa.Column("volume_24h_usd", usd_type),
    # On-chain getReserves at `reserve_block` (V2 pools only).
    sa.Column("reserve_block", mysql.BIGINT(unsigned=True)),
    sa.Column("reserve_token_raw", digits78),
    sa.Column("reserve_quote_raw", digits78),
    sa.Column("onchain_price_quote", price_type),
    sa.Column("deviation_pct", mysql.DECIMAL(12, 4)),
    sa.Index("ix_pool_snapshot_time", "pool_address", "fetched_at_utc"),
    **options,
)

pool_ohlcv = sa.Table(
    "pool_ohlcv",
    metadata,
    sa.Column("pool_address", ascii42, primary_key=True),
    sa.Column("timeframe", sa.String(8), primary_key=True),
    sa.Column("bucket_start_utc", mysql.DATETIME(), primary_key=True),
    sa.Column("open_usd", price_type, nullable=False),
    sa.Column("high_usd", price_type, nullable=False),
    sa.Column("low_usd", price_type, nullable=False),
    sa.Column("close_usd", price_type, nullable=False),
    sa.Column("volume_usd", usd_type, nullable=False),
    sa.Column("fetched_at_utc", mysql.DATETIME(fsp=6), nullable=False),
    **options,
)
