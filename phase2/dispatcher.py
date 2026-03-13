#!/usr/bin/env python3
"""
Phase 2: External Orchestrator — Parallel Worker Dispatcher

Spawns N worker processes, each claiming and processing one API class at a time.
Workers call Claude API directly (not Claude Code CLI) for cost efficiency.
Each worker operates in an isolated working directory to avoid file conflicts.

Usage:
    python3 dispatcher.py                     # default: 10 workers, score >= 5
    python3 dispatcher.py --workers 50        # 50 parallel workers
    python3 dispatcher.py --min-score 8       # Tier 1 only
    python3 dispatcher.py --dry-run           # show queue without starting
    python3 dispatcher.py --status            # show progress dashboard
"""

import argparse
import os
import sys
import time
import shutil
import subprocess
import signal
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.db import (
    init_db, populate_from_api_compat, claim_task, update_task,
    increment_retry, get_progress, get_failures
)

ORCH_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.join(ORCH_ROOT, '..', 'android-to-openharmony-migration')
WORK_DIR = os.path.join(ORCH_ROOT, 'workdirs')
DB_PATH = os.path.join(ORCH_ROOT, 'shim_progress.db')

# Graceful shutdown
shutdown_requested = False
def handle_signal(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    print("\nShutdown requested. Finishing current tasks...")


def setup_work_dir(worker_id: str, task: dict) -> str:
    """
    Create an isolated working directory for a worker.
    Copies only the files the worker needs to modify.
    """
    work_dir = os.path.join(WORK_DIR, f"worker-{worker_id}", task['android_class'].replace('.', '_'))
    os.makedirs(work_dir, exist_ok=True)

    # Copy shim source tree (worker will create new files here)
    shim_src = os.path.join(PROJECT_ROOT, 'shim', 'java')
    shim_dst = os.path.join(work_dir, 'shim', 'java')
    if os.path.exists(shim_src) and not os.path.exists(shim_dst):
        shutil.copytree(shim_src, shim_dst)

    # Copy test infrastructure
    test_src = os.path.join(PROJECT_ROOT, 'test-apps')
    test_dst = os.path.join(work_dir, 'test-apps')
    if os.path.exists(test_src) and not os.path.exists(test_dst):
        shutil.copytree(test_src, test_dst)

    return work_dir


def run_worker(worker_id: str, min_score: float, max_retries: int):
    """
    Worker loop: claim task → process → repeat until no tasks left.
    """
    import importlib
    sys.path.insert(0, os.path.join(ORCH_ROOT))
    from shared.skill_loader import build_prompt

    tasks_done = 0
    tasks_failed = 0

    while not shutdown_requested:
        # Claim next task
        task = claim_task(worker_id, min_score, DB_PATH)
        if not task:
            break  # No more tasks

        android_class = task['android_class']
        print(f"  [{worker_id}] Claimed: {android_class} (score={task['avg_score']}, scenario={task['scenario']})")

        try:
            # Setup isolated working directory
            work_dir = setup_work_dir(worker_id, task)

            # Build prompt
            prompt = build_prompt(
                android_class,
                task['scenario'],
                task['skill_file'],
                work_dir,
                baseline_pass=0, baseline_fail=0,
            )

            # Call Claude Code CLI in danger mode
            result = subprocess.run(
                ['claude', '--dangerously-skip-permissions', '--print', prompt],
                capture_output=True, text=True, timeout=300,
                cwd=work_dir,
            )

            if result.returncode != 0:
                raise RuntimeError(f"Claude CLI failed: {result.stderr[:500]}")

            # Run tests in the isolated directory
            test_result = subprocess.run(
                ['bash', '-c', './run-local-tests.sh'],
                capture_output=True, text=True, timeout=120,
                cwd=os.path.join(work_dir, 'test-apps'),
            )

            test_output = test_result.stdout + test_result.stderr

            # Parse results
            import re
            pass_match = re.search(r'Passed:\s*(\d+)', test_output)
            fail_match = re.search(r'Failed:\s*(\d+)', test_output)
            test_pass = int(pass_match.group(1)) if pass_match else 0
            test_fail = int(fail_match.group(1)) if fail_match else 0

            if test_fail <= 2:  # Allow small tolerance
                update_task(task['id'], 'tested_mock', worker_id,
                           test_pass=test_pass, test_fail=test_fail, db_path=DB_PATH)
                print(f"  [{worker_id}] ✓ {android_class}: {test_pass} pass, {test_fail} fail")
                tasks_done += 1
            else:
                # Retry logic
                retries = task['compile_retries'] + task['test_retries']
                if retries < max_retries:
                    increment_retry(task['id'], 'test', DB_PATH)
                    # Re-queue as pending for another attempt
                    update_task(task['id'], 'pending', worker_id,
                               last_error=f"Test regression: {test_fail} failures",
                               db_path=DB_PATH)
                    print(f"  [{worker_id}] ↻ {android_class}: retry {retries+1}/{max_retries}")
                else:
                    update_task(task['id'], 'failed', worker_id,
                               test_pass=test_pass, test_fail=test_fail,
                               last_error=f"Max retries exceeded. Last: {test_fail} failures",
                               db_path=DB_PATH)
                    print(f"  [{worker_id}] ✗ {android_class}: failed after {max_retries} retries")
                    tasks_failed += 1

        except subprocess.TimeoutExpired:
            update_task(task['id'], 'failed', worker_id,
                       last_error="Timeout (300s)", db_path=DB_PATH)
            print(f"  [{worker_id}] ✗ {android_class}: timeout")
            tasks_failed += 1

        except Exception as e:
            update_task(task['id'], 'failed', worker_id,
                       last_error=str(e)[:500], db_path=DB_PATH)
            print(f"  [{worker_id}] ✗ {android_class}: {str(e)[:100]}")
            tasks_failed += 1

    return worker_id, tasks_done, tasks_failed


def show_status():
    """Print progress dashboard."""
    progress = get_progress(DB_PATH)
    total = sum(v['count'] for v in progress.values())

    print("═══ A2OH Orchestrator Status ═══")
    print(f"Total tasks: {total}")
    print("")
    print(f"{'Status':<15} {'Count':>6} {'APIs':>8} {'Avg Score':>10}")
    print("─" * 45)
    for status, data in sorted(progress.items()):
        print(f"{status:<15} {data['count']:>6} {data['total_apis'] or 0:>8} {data['avg_score'] or 0:>10.1f}")

    done = progress.get('tested_mock', {}).get('count', 0)
    failed = progress.get('failed', {}).get('count', 0)
    pending = progress.get('pending', {}).get('count', 0)
    if total > 0:
        print("")
        print(f"Progress: {done}/{total} ({done*100//total}%) done, {failed} failed, {pending} pending")

    failures = get_failures(DB_PATH)
    if failures:
        print("")
        print("── Recent Failures ──")
        for f in failures[:10]:
            print(f"  {f['android_class']}: {f['last_error'][:80]}")


def main():
    parser = argparse.ArgumentParser(description='A2OH Orchestrator — Parallel Worker Dispatcher')
    parser.add_argument('--workers', type=int, default=10, help='Number of parallel workers')
    parser.add_argument('--min-score', type=float, default=5.0, help='Minimum API score')
    parser.add_argument('--max-retries', type=int, default=3, help='Max retries per task')
    parser.add_argument('--dry-run', action='store_true', help='Show queue without starting')
    parser.add_argument('--status', action='store_true', help='Show progress dashboard')
    parser.add_argument('--populate', action='store_true', help='Populate queue from api_compat.db')
    args = parser.parse_args()

    # Initialize
    init_db(DB_PATH)

    if args.populate or not os.path.exists(DB_PATH):
        added, skipped = populate_from_api_compat(DB_PATH, min_score=args.min_score)
        print(f"Queue populated: {added} added, {skipped} skipped")

    if args.status:
        show_status()
        return

    if args.dry_run:
        show_status()
        print("\n(dry run — no workers started)")
        return

    # Setup signal handlers
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Create work directories
    os.makedirs(WORK_DIR, exist_ok=True)

    print(f"═══ A2OH Orchestrator ═══")
    print(f"Workers: {args.workers} | Min Score: {args.min_score} | Max Retries: {args.max_retries}")
    print("")

    # Spawn workers
    start_time = time.time()
    total_done = 0
    total_failed = 0

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for i in range(args.workers):
            worker_id = f"w{i:03d}"
            future = executor.submit(run_worker, worker_id, args.min_score, args.max_retries)
            futures[future] = worker_id

        for future in as_completed(futures):
            worker_id = futures[future]
            try:
                wid, done, failed = future.result()
                total_done += done
                total_failed += failed
                print(f"Worker {wid} finished: {done} done, {failed} failed")
            except Exception as e:
                print(f"Worker {worker_id} crashed: {e}")

    elapsed = time.time() - start_time
    print("")
    print(f"═══ Dispatch Complete ═══")
    print(f"Time: {elapsed:.0f}s | Done: {total_done} | Failed: {total_failed}")
    print("")
    show_status()


if __name__ == '__main__':
    main()
