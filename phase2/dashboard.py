#!/usr/bin/env python3
"""
Live progress dashboard for the A2OH orchestrator.

Shows real-time task status, worker activity, and progress metrics.
Refreshes every 5 seconds.

Usage:
    python3 dashboard.py              # live dashboard (refreshes)
    python3 dashboard.py --once       # print once and exit
    python3 dashboard.py --json       # JSON output for programmatic use
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.db import get_db

ORCH_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ORCH_ROOT, 'shim_progress.db')


def get_stats():
    """Collect all dashboard statistics."""
    if not os.path.exists(DB_PATH):
        return None

    with get_db(DB_PATH) as db:
        # Overall status
        status_rows = db.execute("""
            SELECT status, COUNT(*) as count, SUM(api_count) as apis,
                   ROUND(AVG(avg_score), 1) as avg_score
            FROM tasks GROUP BY status
        """).fetchall()

        # Scenario breakdown
        scenario_rows = db.execute("""
            SELECT scenario, status, COUNT(*) as count
            FROM tasks GROUP BY scenario, status
            ORDER BY scenario, status
        """).fetchall()

        # Active workers
        active_rows = db.execute("""
            SELECT claimed_by, android_class, claimed_at,
                   ROUND((julianday('now') - julianday(claimed_at)) * 86400) as elapsed_sec
            FROM tasks WHERE status='claimed' AND claimed_by IS NOT NULL
            ORDER BY claimed_at DESC
        """).fetchall()

        # Recent completions
        recent_rows = db.execute("""
            SELECT android_class, status, test_pass, test_fail, completed_at
            FROM tasks WHERE completed_at IS NOT NULL
            ORDER BY completed_at DESC LIMIT 10
        """).fetchall()

        # Recent failures
        failure_rows = db.execute("""
            SELECT android_class, avg_score, scenario, last_error,
                   compile_retries + test_retries as total_retries
            FROM tasks WHERE status='failed'
            ORDER BY updated_at DESC LIMIT 10
        """).fetchall()

        # Per-package progress
        package_rows = db.execute("""
            SELECT package,
                   COUNT(*) as total,
                   SUM(CASE WHEN status='tested_mock' THEN 1 ELSE 0 END) as done,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending
            FROM tasks GROUP BY package ORDER BY total DESC
        """).fetchall()

    return {
        'status': [dict(r) for r in status_rows],
        'scenarios': [dict(r) for r in scenario_rows],
        'active': [dict(r) for r in active_rows],
        'recent': [dict(r) for r in recent_rows],
        'failures': [dict(r) for r in failure_rows],
        'packages': [dict(r) for r in package_rows],
    }


def render_dashboard(stats):
    """Render the dashboard to terminal."""
    if not stats:
        print("No database found. Run dispatcher.py --populate first.")
        return

    # Header
    total = sum(s['count'] for s in stats['status'])
    done = sum(s['count'] for s in stats['status'] if s['status'] == 'tested_mock')
    failed = sum(s['count'] for s in stats['status'] if s['status'] == 'failed')
    pending = sum(s['count'] for s in stats['status'] if s['status'] == 'pending')
    claimed = sum(s['count'] for s in stats['status'] if s['status'] == 'claimed')
    total_apis = sum(s['apis'] or 0 for s in stats['status'])

    pct = done * 100 // max(total, 1)
    bar_width = 40
    filled = pct * bar_width // 100
    bar = '█' * filled + '░' * (bar_width - filled)

    print("═══════════════════════════════════════════════════════════")
    print("  A2OH Orchestrator Dashboard")
    print("═══════════════════════════════════════════════════════════")
    print(f"  [{bar}] {pct}%")
    print(f"  {done}/{total} classes done | {total_apis} total APIs")
    print(f"  Pending: {pending} | Active: {claimed} | Failed: {failed}")
    print("")

    # Status table
    print("── Status Breakdown ──")
    print(f"  {'Status':<15} {'Count':>6} {'APIs':>8} {'Avg Score':>10}")
    print(f"  {'─'*45}")
    for s in stats['status']:
        print(f"  {s['status']:<15} {s['count']:>6} {s['apis'] or 0:>8} {s['avg_score'] or 0:>10.1f}")
    print("")

    # Active workers
    if stats['active']:
        print("── Active Workers ──")
        for w in stats['active']:
            elapsed = w['elapsed_sec'] or 0
            print(f"  {w['claimed_by']}: {w['android_class']} ({elapsed:.0f}s)")
        print("")

    # Per-package progress
    print("── Package Progress ──")
    print(f"  {'Package':<25} {'Done':>5} {'Fail':>5} {'Pend':>5} {'Total':>6}")
    print(f"  {'─'*50}")
    for p in stats['packages']:
        print(f"  {p['package']:<25} {p['done']:>5} {p['failed']:>5} {p['pending']:>5} {p['total']:>6}")
    print("")

    # Recent completions
    if stats['recent']:
        print("── Recent Activity ──")
        for r in stats['recent'][:5]:
            icon = '✓' if r['status'] == 'tested_mock' else '✗'
            print(f"  {icon} {r['android_class']}: {r['test_pass']}P/{r['test_fail']}F @ {r['completed_at']}")
        print("")

    # Failures
    if stats['failures']:
        print("── Failures ──")
        for f in stats['failures'][:5]:
            err = (f['last_error'] or '')[:60]
            print(f"  {f['android_class']} ({f['scenario']}): {err}")
        print("")


def main():
    parser = argparse.ArgumentParser(description='A2OH Orchestrator Dashboard')
    parser.add_argument('--once', action='store_true', help='Print once and exit')
    parser.add_argument('--json', action='store_true', help='JSON output')
    parser.add_argument('--interval', type=int, default=5, help='Refresh interval (seconds)')
    args = parser.parse_args()

    if args.json:
        stats = get_stats()
        print(json.dumps(stats, indent=2, default=str))
        return

    if args.once:
        render_dashboard(get_stats())
        return

    # Live refresh
    try:
        while True:
            os.system('clear' if os.name != 'nt' else 'cls')
            render_dashboard(get_stats())
            print(f"  Refreshing every {args.interval}s... (Ctrl+C to stop)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == '__main__':
    main()
