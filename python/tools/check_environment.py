"""Check installed project tools without using RPC or database credentials."""

import argparse
import importlib
import importlib.metadata
import json
import re
import sys
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMPORTS = (
    "httpx",
    "pymysql",
    "cryptography",
    "sqlalchemy",
    "alembic",
    "pydantic",
    "pydantic_settings",
    "dotenv",
    "tenacity",
    "rich",
    "structlog",
    "web3",
    "typer",
    "numpy",
    "pandas",
    "pyarrow",
    "duckdb",
    "matplotlib",
    "plotly",
    "openpyxl",
    "jupyterlab",
    "ipykernel",
    "fastapi",
    "uvicorn",
    "pytest",
    "pytest_cov",
    "respx",
    "ruff",
    "mypy",
    "pre_commit",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check complete project dependencies")
    parser.add_argument("--output", type=Path, help="Optional report destination")
    args = parser.parse_args()
    checked_at = datetime.now(UTC)
    checked_on = checked_at.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    groups = {"core": config["project"]["dependencies"]}
    groups.update(config["project"]["optional-dependencies"])
    packages = {}
    errors = []
    for requirements in groups.values():
        for requirement in requirements:
            name = re.match(r"[A-Za-z0-9_.-]+", requirement).group()
            try:
                packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                errors.append(f"Missing distribution: {name}")
    for name in IMPORTS:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 -- collect every package import failure in this diagnostic
            errors.append(f"Import failed: {name}: {type(exc).__name__}: {exc}")
    database_dialect = None
    try:
        sqlalchemy = importlib.import_module("sqlalchemy")
        engine = sqlalchemy.create_engine("mysql+pymysql://", pool_pre_ping=True)
        database_dialect = f"{engine.dialect.name}+{engine.dialect.driver}"
        engine.dispose()
    except Exception as exc:  # noqa: BLE001 -- report dialect initialization errors without connecting
        errors.append(f"Dialect initialization failed: {type(exc).__name__}: {exc}")
    report = {
        "checked_on": checked_on,
        "checked_at_utc": checked_at.isoformat(),
        "date_timezone": "Asia/Shanghai",
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "direct_package_count": len(packages),
        "installed_distribution_count": len(list(importlib.metadata.distributions())),
        "checked_import_count": len(IMPORTS),
        "database_dialect": database_dialect,
        "versions": packages,
        "errors": errors,
        "status": "passed" if not errors else "failed",
        "scope": "Package versions, imports and MySQL dialect initialization; no database or RPC connection tested",
    }
    target = (
        args.output or ROOT / "reports" / f"environment-{checked_on}.json"
    )
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
