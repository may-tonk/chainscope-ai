"""SQLAlchemy Core schema. Initial DDL is frozen separately in Alembic."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

metadata = sa.MetaData()
ascii42 = mysql.CHAR(42, charset="ascii", collation="ascii_bin")
ascii66 = mysql.CHAR(66, charset="ascii", collation="ascii_bin")
batch_id_type = mysql.CHAR(36, charset="ascii", collation="ascii_bin")
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
