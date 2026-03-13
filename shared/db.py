"""
Task queue operations on shim_progress.db.

Provides atomic claim/update/query with proper locking for concurrent workers.
"""

import sqlite3
import os
import time
from contextlib import contextmanager
from typing import Optional

DEFAULT_DB = os.path.join(os.path.dirname(__file__), '..', 'shim_progress.db')
API_COMPAT_DB = os.path.join(os.path.dirname(__file__), '..', '..',
                              'android-to-openharmony-migration', 'database', 'api_compat.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    android_class TEXT UNIQUE NOT NULL,
    package TEXT NOT NULL,
    api_count INTEGER DEFAULT 0,
    avg_score REAL DEFAULT 0,
    scenario TEXT,              -- S1-S8
    skill_file TEXT,            -- path to per-API skill file
    status TEXT DEFAULT 'pending',
    -- pending | claimed | compiling | testing | tested_mock | failed | skipped
    claimed_by TEXT,            -- worker ID
    claimed_at TIMESTAMP,
    compile_retries INTEGER DEFAULT 0,
    test_retries INTEGER DEFAULT 0,
    test_pass INTEGER DEFAULT 0,
    test_fail INTEGER DEFAULT 0,
    last_error TEXT,
    work_dir TEXT,              -- isolated working directory
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    android_class TEXT,
    action TEXT,                -- claimed | compile_ok | compile_fail | test_ok | test_fail | done | failed
    worker_id TEXT,
    details TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_class ON tasks(android_class);
"""

# Already-shimmed classes (from existing shim/java/)
ALREADY_SHIMMED = {
    'android.util.Log', 'android.os.Bundle', 'android.os.Build',
    'android.content.SharedPreferences', 'android.content.SharedPreferences.Editor',
    'android.content.Intent', 'android.content.Context',
    'android.content.ContentValues', 'android.content.BroadcastReceiver',
    'android.app.Activity', 'android.app.NotificationManager',
    'android.app.NotificationChannel', 'android.app.Notification',
    'android.app.Notification.Builder', 'android.app.AlarmManager',
    'android.app.PendingIntent', 'android.database.sqlite.SQLiteDatabase',
    'android.database.sqlite.SQLiteOpenHelper', 'android.database.Cursor',
    'android.database.CursorWrapper', 'android.database.SQLException',
    'android.net.Uri', 'android.widget.Toast', 'android.widget.TextView',
    'android.widget.Button', 'android.widget.EditText',
    'android.widget.ImageView', 'android.widget.LinearLayout',
    'android.widget.FrameLayout', 'android.widget.ScrollView',
    'android.widget.ListView', 'android.widget.CheckBox',
    'android.widget.Switch', 'android.widget.SeekBar',
    'android.widget.ProgressBar', 'android.view.View',
    'android.view.ViewGroup', 'android.view.Gravity',
    'android.view.LayoutInflater',
}


@contextmanager
def get_db(db_path=DEFAULT_DB):
    """Get a database connection with WAL mode for concurrent access."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=DEFAULT_DB):
    """Initialize the task queue database."""
    with get_db(db_path) as db:
        db.executescript(SCHEMA)


def populate_from_api_compat(db_path=DEFAULT_DB, api_db_path=API_COMPAT_DB, min_score=5):
    """
    Populate task queue from api_compat.db.
    Only adds classes not already in the queue and not already shimmed.
    """
    init_db(db_path)

    api_db = sqlite3.connect(api_db_path)
    api_db.row_factory = sqlite3.Row

    classes = api_db.execute("""
        SELECT p.name || '.' || t.full_name AS fqn,
               p.name AS package,
               COUNT(*) AS api_count,
               ROUND(AVG(m.score), 1) AS avg_score,
               SUM(CASE WHEN m.mapping_type IN ('direct','near') THEN 1 ELSE 0 END) AS direct_near,
               SUM(CASE WHEN m.needs_ui_rewrite THEN 1 ELSE 0 END) AS ui_count,
               SUM(CASE WHEN m.paradigm_shift THEN 1 ELSE 0 END) AS async_count,
               SUM(CASE WHEN m.needs_native THEN 1 ELSE 0 END) AS native_count,
               GROUP_CONCAT(DISTINCT m.mapping_type) AS types
        FROM api_mappings m
        JOIN android_apis a ON m.android_api_id = a.id
        JOIN android_types t ON a.type_id = t.id
        JOIN android_packages p ON t.package_id = p.id
        WHERE a.kind IN ('method','constructor')
          AND p.name IN ('android.app','android.content','android.os','android.database',
                         'android.net','android.util','android.widget','android.view',
                         'android.media','android.graphics','android.text','android.telephony',
                         'android.bluetooth','android.hardware','android.location','android.provider')
        GROUP BY fqn
        HAVING api_count >= 3
        ORDER BY avg_score DESC, api_count DESC
    """).fetchall()
    api_db.close()

    added = 0
    skipped = 0
    with get_db(db_path) as db:
        for row in classes:
            fqn = row['fqn']

            # Skip already shimmed or generic types
            if fqn in ALREADY_SHIMMED or '<' in fqn or '$' in fqn:
                skipped += 1
                continue

            # Classify scenario
            total = row['api_count']
            scenario = classify_scenario(
                row['direct_near'], total, row['avg_score'],
                row['ui_count'], row['async_count'], row['native_count'],
                row['types']
            )

            # Skill file path
            safe_name = fqn.replace('.', '_').replace('<', '').replace('>', '')
            skill_file = f"{safe_name}.md"

            try:
                db.execute("""
                    INSERT OR IGNORE INTO tasks (android_class, package, api_count, avg_score, scenario, skill_file)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (fqn, row['package'], total, row['avg_score'], scenario, skill_file))
                if db.execute("SELECT changes()").fetchone()[0] > 0:
                    added += 1
            except sqlite3.IntegrityError:
                skipped += 1

    return added, skipped


def classify_scenario(direct_near, total, avg_score, ui_count, async_count, native_count, types):
    """Classify task scenario based on API characteristics."""
    if ui_count > total * 0.5:
        return 'S6'
    if async_count > total * 0.3 and ui_count <= total * 0.3:
        return 'S7'
    if native_count > total * 0.5:
        return 'S5'
    if direct_near > total * 0.5 and avg_score >= 8:
        return 'S1'
    if direct_near > total * 0.5 and avg_score >= 7:
        return 'S2'
    if 'composite' in (types or '') and avg_score >= 5:
        return 'S4'
    if 'partial' in (types or '') and avg_score >= 5:
        return 'S3'
    if avg_score < 3:
        return 'S8'
    return 'S3'


def claim_task(worker_id: str, min_score: float = 5.0, db_path=DEFAULT_DB) -> Optional[dict]:
    """
    Atomically claim the next available task for a worker.
    Returns task dict or None if no tasks available.
    """
    with get_db(db_path) as db:
        # Release stale claims (worker died)
        db.execute("""
            UPDATE tasks SET status='pending', claimed_by=NULL, claimed_at=NULL
            WHERE status='claimed'
              AND claimed_at < datetime('now', '-10 minutes')
        """)

        # Claim highest-priority pending task
        task = db.execute("""
            SELECT * FROM tasks
            WHERE status='pending' AND avg_score >= ?
            ORDER BY avg_score DESC, api_count DESC
            LIMIT 1
        """, (min_score,)).fetchone()

        if not task:
            return None

        db.execute("""
            UPDATE tasks SET status='claimed', claimed_by=?, claimed_at=datetime('now'),
                             updated_at=datetime('now')
            WHERE id=? AND status='pending'
        """, (worker_id, task['id']))

        # Verify we got the claim (another worker might have beaten us)
        claimed = db.execute("""
            SELECT * FROM tasks WHERE id=? AND claimed_by=?
        """, (task['id'], worker_id)).fetchone()

        if claimed:
            db.execute("""
                INSERT INTO task_log (task_id, android_class, action, worker_id)
                VALUES (?, ?, 'claimed', ?)
            """, (claimed['id'], claimed['android_class'], worker_id))
            return dict(claimed)

        return None


def update_task(task_id: int, status: str, worker_id: str,
                test_pass=0, test_fail=0, last_error=None, db_path=DEFAULT_DB):
    """Update task status after worker completes a step."""
    with get_db(db_path) as db:
        updates = {
            'status': status,
            'test_pass': test_pass,
            'test_fail': test_fail,
            'updated_at': 'datetime("now")',
        }
        if last_error:
            updates['last_error'] = last_error
        if status in ('tested_mock', 'failed'):
            updates['completed_at'] = 'datetime("now")'

        db.execute("""
            UPDATE tasks SET status=?, test_pass=?, test_fail=?, last_error=?,
                             updated_at=datetime('now'),
                             completed_at=CASE WHEN ? IN ('tested_mock','failed')
                                               THEN datetime('now') ELSE completed_at END
            WHERE id=?
        """, (status, test_pass, test_fail, last_error, status, task_id))

        db.execute("""
            INSERT INTO task_log (task_id, android_class, action, worker_id, details)
            VALUES (?, (SELECT android_class FROM tasks WHERE id=?), ?, ?, ?)
        """, (task_id, task_id, status, worker_id, last_error))


def increment_retry(task_id: int, retry_type: str, db_path=DEFAULT_DB):
    """Increment compile or test retry counter."""
    col = 'compile_retries' if retry_type == 'compile' else 'test_retries'
    with get_db(db_path) as db:
        db.execute(f"""
            UPDATE tasks SET {col} = {col} + 1, updated_at=datetime('now')
            WHERE id=?
        """, (task_id,))


def get_progress(db_path=DEFAULT_DB) -> dict:
    """Get overall progress summary."""
    with get_db(db_path) as db:
        rows = db.execute("""
            SELECT status, COUNT(*) as count,
                   ROUND(AVG(avg_score), 1) as avg_score,
                   SUM(api_count) as total_apis
            FROM tasks GROUP BY status ORDER BY count DESC
        """).fetchall()
        return {row['status']: dict(row) for row in rows}


def get_failures(db_path=DEFAULT_DB) -> list:
    """Get all failed tasks with error details."""
    with get_db(db_path) as db:
        return [dict(r) for r in db.execute("""
            SELECT android_class, avg_score, scenario, last_error,
                   compile_retries, test_retries
            FROM tasks WHERE status='failed'
            ORDER BY avg_score DESC
        """).fetchall()]
