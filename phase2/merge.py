#!/usr/bin/env python3
"""
Merge worker results from isolated working directories into the main repo.

After workers generate shim code in their isolated dirs, this script:
1. Scans all workdirs for new/modified files
2. Copies new shim classes to the main repo
3. Aggregates OHBridge additions (dedup)
4. Aggregates test additions (dedup)
5. Runs the full test suite to verify no conflicts
6. Optionally commits the merged result

Usage:
    python3 merge.py                   # merge all completed workdirs
    python3 merge.py --dry-run         # show what would be merged
    python3 merge.py --commit          # merge and git commit
"""

import argparse
import os
import sys
import shutil
import subprocess
import re
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.db import get_db

ORCH_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.join(ORCH_ROOT, '..', 'android-to-openharmony-migration')
WORK_DIR = os.path.join(ORCH_ROOT, 'workdirs')
DB_PATH = os.path.join(ORCH_ROOT, 'shim_progress.db')


def find_completed_workdirs():
    """Find all workdirs for tasks that passed tests."""
    completed = []
    with get_db(DB_PATH) as db:
        tasks = db.execute("""
            SELECT android_class, work_dir FROM tasks
            WHERE status='tested_mock' AND work_dir IS NOT NULL
        """).fetchall()
        for task in tasks:
            if task['work_dir'] and os.path.exists(task['work_dir']):
                completed.append(dict(task))
    return completed


def find_new_shim_files(work_dir):
    """Find new Java shim files in a worker's directory."""
    shim_dir = os.path.join(work_dir, 'shim', 'java')
    if not os.path.exists(shim_dir):
        return []

    new_files = []
    for root, dirs, files in os.walk(shim_dir):
        for f in files:
            if f.endswith('.java'):
                src = os.path.join(root, f)
                rel = os.path.relpath(src, os.path.join(work_dir, 'shim', 'java'))
                dst = os.path.join(PROJECT_ROOT, 'shim', 'java', rel)
                # Only include if file is new or different
                if not os.path.exists(dst):
                    new_files.append((src, dst, rel))
                else:
                    with open(src) as a, open(dst) as b:
                        if a.read() != b.read():
                            new_files.append((src, dst, rel))
    return new_files


def extract_ohbridge_additions(work_dir):
    """Extract new methods added to OHBridge.java by a worker."""
    bridge_path = os.path.join(work_dir, 'shim', 'java', 'com', 'ohos', 'shim', 'bridge', 'OHBridge.java')
    original_path = os.path.join(PROJECT_ROOT, 'shim', 'java', 'com', 'ohos', 'shim', 'bridge', 'OHBridge.java')

    if not os.path.exists(bridge_path) or not os.path.exists(original_path):
        return []

    with open(original_path) as f:
        original_methods = set(re.findall(r'public static (?:native )?.*?\b(\w+)\s*\(', f.read()))

    with open(bridge_path) as f:
        worker_content = f.read()
        worker_methods = re.findall(r'(public static (?:native )?[^}]+?;)', worker_content, re.DOTALL)

    new_methods = []
    for method in worker_methods:
        # Extract method name
        name_match = re.search(r'\b(\w+)\s*\(', method)
        if name_match and name_match.group(1) not in original_methods:
            new_methods.append(method.strip())

    return new_methods


def extract_test_additions(work_dir):
    """Extract new test methods added to HeadlessTest.java by a worker."""
    test_path = os.path.join(work_dir, 'test-apps', '02-headless-cli', 'src', 'HeadlessTest.java')
    original_path = os.path.join(PROJECT_ROOT, 'test-apps', '02-headless-cli', 'src', 'HeadlessTest.java')

    if not os.path.exists(test_path) or not os.path.exists(original_path):
        return []

    with open(original_path) as f:
        original_methods = set(re.findall(r'static void (test\w+)\s*\(', f.read()))

    with open(test_path) as f:
        worker_content = f.read()

    new_methods = []
    # Find new test method blocks
    pattern = r'(static void (test\w+)\s*\(\)\s*\{[^}]+(?:\{[^}]*\}[^}]*)*\})'
    for match in re.finditer(pattern, worker_content, re.DOTALL):
        method_name = match.group(2)
        if method_name not in original_methods:
            new_methods.append(match.group(1).strip())

    return new_methods


def merge_all(dry_run=False, commit=False):
    """Merge all completed worker results into the main repo."""
    completed = find_completed_workdirs()
    if not completed:
        print("No completed workdirs to merge.")
        return

    print(f"Found {len(completed)} completed tasks to merge.")
    print("")

    all_new_files = []
    all_bridge_methods = []
    all_test_methods = []
    all_mock_methods = []

    for task in completed:
        work_dir = task['work_dir']
        cls = task['android_class']
        print(f"  Scanning: {cls}")

        # New shim files
        new_files = find_new_shim_files(work_dir)
        for src, dst, rel in new_files:
            print(f"    + shim/java/{rel}")
            all_new_files.append((src, dst))

        # OHBridge additions
        bridge_methods = extract_ohbridge_additions(work_dir)
        for m in bridge_methods:
            print(f"    + OHBridge method: {m[:60]}...")
            all_bridge_methods.append(m)

        # Test additions
        test_methods = extract_test_additions(work_dir)
        for m in test_methods:
            name = re.search(r'(test\w+)', m)
            print(f"    + Test: {name.group(1) if name else '?'}")
            all_test_methods.append(m)

    print("")
    print(f"Total: {len(all_new_files)} new files, {len(all_bridge_methods)} bridge methods, {len(all_test_methods)} tests")

    if dry_run:
        print("\n(dry run — nothing written)")
        return

    # 1. Copy new shim files
    for src, dst in all_new_files:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  Copied: {os.path.relpath(dst, PROJECT_ROOT)}")

    # 2. Append OHBridge methods (dedup by method name)
    if all_bridge_methods:
        bridge_path = os.path.join(PROJECT_ROOT, 'shim', 'java', 'com', 'ohos', 'shim', 'bridge', 'OHBridge.java')
        with open(bridge_path) as f:
            content = f.read()

        # Find insertion point (before last closing brace)
        insert_pos = content.rfind('}')
        additions = "\n    // ── Auto-merged by A2OH Orchestrator ──\n"
        seen = set()
        for method in all_bridge_methods:
            name = re.search(r'\b(\w+)\s*\(', method)
            if name and name.group(1) not in seen:
                seen.add(name.group(1))
                additions += f"\n    {method}\n"

        content = content[:insert_pos] + additions + "\n" + content[insert_pos:]
        with open(bridge_path, 'w') as f:
            f.write(content)
        print(f"  Updated OHBridge.java: +{len(seen)} methods")

    # 3. Append test methods
    if all_test_methods:
        test_path = os.path.join(PROJECT_ROOT, 'test-apps', '02-headless-cli', 'src', 'HeadlessTest.java')
        with open(test_path) as f:
            content = f.read()

        insert_pos = content.rfind('}')
        additions = "\n    // ── Auto-merged tests by A2OH Orchestrator ──\n"
        seen = set()
        for method in all_test_methods:
            name = re.search(r'(test\w+)', method)
            if name and name.group(1) not in seen:
                seen.add(name.group(1))
                additions += f"\n    {method}\n"

        content = content[:insert_pos] + additions + "\n" + content[insert_pos:]
        with open(test_path, 'w') as f:
            f.write(content)
        print(f"  Updated HeadlessTest.java: +{len(seen)} tests")

    # 4. Run full test suite
    print("")
    print("Running full test suite...")
    result = subprocess.run(
        ['bash', '-c', './run-local-tests.sh'],
        capture_output=True, text=True, timeout=120,
        cwd=os.path.join(PROJECT_ROOT, 'test-apps'),
    )
    print(result.stdout[-500:] if result.stdout else "(no output)")
    if result.returncode != 0:
        print("WARNING: Tests failed after merge. Manual review needed.")
        print(result.stderr[-500:] if result.stderr else "")

    # 5. Git commit if requested
    if commit:
        os.chdir(PROJECT_ROOT)
        subprocess.run(['git', 'add', 'shim/', 'test-apps/'], check=True)
        n = len(all_new_files)
        msg = f"Add {n} AI-generated shim classes ({len(all_bridge_methods)} bridge methods, {len(all_test_methods)} tests)"
        subprocess.run(['git', 'commit', '-m', msg], check=True)
        print(f"\nCommitted: {msg}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Merge worker results into main repo')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--commit', action='store_true')
    args = parser.parse_args()
    merge_all(dry_run=args.dry_run, commit=args.commit)
