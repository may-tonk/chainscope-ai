"""DEX pools and swaps, address balances and labels, market snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

ascii42 = mysql.CHAR(42, charset="ascii", collation="ascii_bin")
ascii66 = mysql.CHAR(66, charset="ascii", collation="ascii_bin")
job_id_type = mysql.VARCHAR(32, charset="ascii", collation="ascii_bin")
options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
digits78 = mysql.VARCHAR(78, charset="ascii", collation="ascii_bin")
balance_type = mysql.DECIMAL(65, 0)
price_type = mysql.DECIMAL(50, 30)
usd_type = mysql.DECIMAL(38, 2)


def upgrade():
    op.add_column(
        "scan_jobs", sa.Column("balances_through", mysql.BIGINT(unsigned=True))
    )
    op.add_column(
        "coverage_windows", sa.Column("swap_count", mysql.INTEGER(unsigned=True))
    )
    op.create_table(
        "dex_pools",
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
    op.create_table(
        "swap_events",
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
    op.create_table(
        "address_balances",
        sa.Column("chain_id", mysql.INTEGER(unsigned=True), primary_key=True),
        sa.Column("token_address", ascii42, primary_key=True),
        sa.Column("address", ascii42, primary_key=True),
        sa.Column("balance_raw", balance_type, nullable=False),
        sa.Column("last_block", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Index("ix_balance_rank", "chain_id", "token_address", "balance_raw"),
        **options,
    )
    op.create_table(
        "address_info",
        sa.Column("chain_id", mysql.INTEGER(unsigned=True), primary_key=True),
        sa.Column("address", ascii42, primary_key=True),
        sa.Column("is_contract", sa.Boolean, nullable=False),
        sa.Column("code_size", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("checked_block", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("checked_at_utc", mysql.DATETIME(fsp=6), nullable=False),
        **options,
    )
    op.create_table(
        "address_labels",
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
    op.create_table(
        "market_snapshots",
        sa.Column(
            "snapshot_id",
            mysql.BIGINT(unsigned=True),
            primary_key=True,
            autoincrement=True,
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
    op.create_table(
        "pool_snapshots",
        sa.Column(
            "snapshot_id",
            mysql.BIGINT(unsigned=True),
            primary_key=True,
            autoincrement=True,
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
    op.create_table(
        "pool_ohlcv",
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


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore from a backup.")


if __name__ == "__main__":
    raise SystemExit("迁移文件由 Alembic 加载，请执行 python -m chainscope init-db")
