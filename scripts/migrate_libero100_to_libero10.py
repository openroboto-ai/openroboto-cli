#!/usr/bin/env python3
"""
数据库迁移脚本: libero_100 -> libero_10

作用:
  1. 将 submissions.env_list 中的 "libero_100" 替换为 "libero_10"
  2. 将 eval_scores.env_name = 'libero_100' 更新为 'libero_10'
  3. 输出迁移统计信息

用法:
  python migrate_libero100_to_libero10.py [--db-path PATH] [--dry-run]

默认数据库路径: data/backend.db (相对于项目根目录)
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timezone


def find_project_root() -> str:
    """Find project root (where protocol/ directory exists)."""
    here = os.path.dirname(os.path.abspath(__file__))
    # Try current dir, then parent
    for p in [here, os.path.dirname(here)]:
        if os.path.isdir(os.path.join(p, "protocol")):
            return p
    return here


def migrate(db_path: str, dry_run: bool = False) -> dict:
    """Run migration. Returns stats dict."""
    stats = {
        "submissions_updated": 0,
        "eval_scores_updated": 0,
        "dry_run": dry_run,
        "db_path": db_path,
    }

    if not os.path.exists(db_path):
        print(f"[!] Database not found: {db_path}")
        print("    No migration needed (empty database).")
        return stats

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── 1. submissions.env_list (JSON TEXT column) ──────────
    cursor = conn.execute(
        "SELECT id, task_id, env_list FROM submissions WHERE env_list LIKE '%libero_100%'"
    )
    rows = cursor.fetchall()
    stats["submissions_updated"] = len(rows)

    if rows:
        print(f"[submissions] Found {len(rows)} record(s) with 'libero_100' in env_list:")
        for r in rows:
            old = r["env_list"]
            new = old.replace("libero_100", "libero_10")
            print(f"  id={r['id']} task={r['task_id']}")
            print(f"    old: {old}")
            print(f"    new: {new}")
            if not dry_run:
                conn.execute(
                    "UPDATE submissions SET env_list = ?, updated_at = ? WHERE id = ?",
                    (new, datetime.now(timezone.utc).isoformat(), r["id"]),
                )

    # ── 2. eval_scores.env_name ─────────────────────────────
    cursor = conn.execute(
        "SELECT id, task_id, env_name FROM eval_scores WHERE env_name = 'libero_100'"
    )
    score_rows = cursor.fetchall()
    stats["eval_scores_updated"] = len(score_rows)

    if score_rows:
        print(f"[eval_scores] Found {len(score_rows)} record(s) with env_name='libero_100':")
        for r in score_rows:
            print(f"  id={r['id']} task={r['task_id']} env_name={r['env_name']} -> libero_10")
            if not dry_run:
                conn.execute(
                    "UPDATE eval_scores SET env_name = 'libero_10' WHERE id = ?",
                    (r["id"],),
                )

    if not dry_run and (rows or score_rows):
        conn.commit()
        print("[✓] Changes committed.")
    elif dry_run:
        print("[dry-run] No changes made.")

    # ── 3. Post-migration verification ──────────────────────
    remaining_sub = conn.execute(
        "SELECT COUNT(*) as cnt FROM submissions WHERE env_list LIKE '%libero_100%'"
    ).fetchone()["cnt"]
    remaining_scores = conn.execute(
        "SELECT COUNT(*) as cnt FROM eval_scores WHERE env_name = 'libero_100'"
    ).fetchone()["cnt"]

    if remaining_sub > 0 or remaining_scores > 0:
        print(f"[!] WARNING: {remaining_sub} submissions + {remaining_scores} eval_scores still have 'libero_100'")
    else:
        print("[✓] Verification passed: no 'libero_100' remaining.")

    conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Migrate libero_100 -> libero_10 in database")
    parser.add_argument("--db-path", default="", help="SQLite database path (default: auto-detect)")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    args = parser.parse_args()

    project_root = find_project_root()
    db_path = args.db_path or os.path.join(project_root, "data", "backend.db")

    print(f"=" * 60)
    print(f"  Migration: libero_100 -> libero_10")
    print(f"  Database:  {db_path}")
    print(f"  Dry run:   {args.dry_run}")
    print(f"=" * 60)
    print()

    stats = migrate(db_path, dry_run=args.dry_run)

    print()
    print(f"Summary:")
    print(f"  submissions.env_list updated: {stats['submissions_updated']}")
    print(f"  eval_scores.env_name updated: {stats['eval_scores_updated']}")
    print(f"  dry_run:                      {stats['dry_run']}")


if __name__ == "__main__":
    main()
