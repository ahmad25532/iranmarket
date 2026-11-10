"""
Daily copy of what cannot be downloaded again: the database (EPS as seen, bill yields, snapshots) and
the portfolio files. Runs after the collector, and a day's folder is replaced by that day's later runs.

Copies go to data/backups unless IRANMARKET_BACKUP_DIR names another folder. Only a folder on another
drive or a synced folder also survives a failed disk.

Usage:  python backup.py
"""

import os
import shutil
import sys
from datetime import date
from typing import List, Optional

import duckdb

import datastore
import sources

KEEP_DAYS = 3
PRIVATE_FILES = ("portfolio.json", "portfolio_history.json", "portfolio_flows.json")
DB_NAME = "market.duckdb"


def _rows(db_path: str) -> int:
    con = duckdb.connect(db_path, read_only=True)
    try:
        return con.execute("SELECT count(*) FROM daily_prices").fetchone()[0]
    finally:
        con.close()


def _is_day_folder(name: str) -> bool:
    try:
        date.fromisoformat(name)
        return True
    except ValueError:
        return False


def backup(db_path: str, dest: str, today: Optional[date] = None, keep: int = KEEP_DAYS) -> str:
    """
    Makes dest/YYYY-MM-DD and returns its path. The old copy is only replaced once the new one opens
    and has the same number of price rows. Raises if the collector holds the database or space is short.
    """
    today = today or sources.tehran_today()
    os.makedirs(dest, exist_ok=True)
    size = os.path.getsize(db_path)
    if shutil.disk_usage(dest).free < 2 * size:
        raise OSError(f"less than {2 * size / 1e9:.1f} GB free in {dest}")

    final = os.path.join(dest, today.isoformat())
    tmp = final + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    # Opening read-only fails while a writer holds the database, and blocks a writer during the copy.
    con = duckdb.connect(db_path, read_only=True)
    try:
        expected = con.execute("SELECT count(*) FROM daily_prices").fetchone()[0]
        shutil.copy2(db_path, os.path.join(tmp, DB_NAME))
    finally:
        con.close()
    copied = _rows(os.path.join(tmp, DB_NAME))
    if copied != expected:
        shutil.rmtree(tmp, ignore_errors=True)
        raise OSError(f"copy has {copied} price rows, expected {expected}")
    for name in PRIVATE_FILES:
        src = os.path.join(os.path.dirname(db_path), name)
        if os.path.exists(src):
            shutil.copy2(src, tmp)

    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    for old in _day_folders(dest)[:-keep]:
        shutil.rmtree(os.path.join(dest, old), ignore_errors=True)
    return final


def _day_folders(dest: str) -> List[str]:
    return sorted(n for n in os.listdir(dest) if _is_day_folder(n))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    dest = os.environ.get("IRANMARKET_BACKUP_DIR") or os.path.join(datastore.BASE_DIR, "data", "backups")
    try:
        print(f"Backup written to {backup(datastore.DEFAULT_DB_PATH, dest)}")
    except (OSError, duckdb.Error) as e:
        print(f"[FAIL] backup: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
