#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Phase 1: Enhanced A2OH Loop — Skill-Enriched, Internal Subagents
#
# Validates the pipeline on a small batch (5-10 classes) using
# Claude Code's internal Agent tool. Each class gets its per-API
# skill file injected into the prompt.
#
# Usage:
#   ./enhanced_loop.sh                    # default: 5 classes, score >= 8
#   ./enhanced_loop.sh --max-classes 10   # process 10 classes
#   ./enhanced_loop.sh --min-score 5      # include Tier 2
#   ./enhanced_loop.sh --dry-run          # show what would be processed
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ORCH_ROOT="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="${ORCH_ROOT}/../android-to-openharmony-migration"
DB="${PROJECT_ROOT}/database/api_compat.db"
PROGRESS_DB="${ORCH_ROOT}/shim_progress.db"
SKILLS_DIR="${PROJECT_ROOT}/skills/per-api"

MAX_CLASSES=5
MIN_SCORE=8
DRY_RUN=false
MAX_RETRIES=3

for arg in "$@"; do
    case $arg in
        --max-classes=*) MAX_CLASSES="${arg#*=}" ;;
        --min-score=*) MIN_SCORE="${arg#*=}" ;;
        --dry-run) DRY_RUN=true ;;
        --help) echo "Usage: $0 [--max-classes=N] [--min-score=N] [--dry-run]"; exit 0 ;;
    esac
done

# ── Initialize orchestrator DB ──
python3 -c "
import sys; sys.path.insert(0, '${ORCH_ROOT}')
from shared.db import populate_from_api_compat
added, skipped = populate_from_api_compat('${PROGRESS_DB}', '${DB}', ${MIN_SCORE})
print(f'Task queue: {added} added, {skipped} skipped')
"

# ── Get candidate classes ──
echo "═══ Phase 1: Skill-Enriched A2OH Loop ═══"
echo "Config: max_classes=${MAX_CLASSES} min_score=${MIN_SCORE} dry_run=${DRY_RUN}"
echo ""

CANDIDATES=$(sqlite3 "${PROGRESS_DB}" "
    SELECT android_class, api_count, avg_score, scenario, skill_file
    FROM tasks
    WHERE status='pending' AND avg_score >= ${MIN_SCORE}
    ORDER BY avg_score DESC, api_count DESC
    LIMIT ${MAX_CLASSES};
")

if [ -z "$CANDIDATES" ]; then
    echo "No pending tasks with score >= ${MIN_SCORE}"
    exit 0
fi

echo "── Candidate Classes ──"
printf "%-45s %5s %6s %4s %s\n" "Class" "APIs" "Score" "Scen" "Skill File"
echo "$CANDIDATES" | while IFS='|' read -r cls count score scenario skill; do
    printf "%-45s %5s %6s %4s %s\n" "$cls" "$count" "$score" "$scenario" "$skill"
done
echo ""

if $DRY_RUN; then
    echo "(dry run — no code generation)"
    exit 0
fi

# ── Get baseline test results ──
echo "Running baseline tests..."
cd "${PROJECT_ROOT}/test-apps"
BASELINE_OUTPUT=$(./run-local-tests.sh 2>&1) || true
BASELINE_PASS=$(echo "$BASELINE_OUTPUT" | grep -oP 'Passed:\s*\K\d+' | head -1 || echo "0")
BASELINE_FAIL=$(echo "$BASELINE_OUTPUT" | grep -oP 'Failed:\s*\K\d+' | head -1 || echo "0")
echo "Baseline: ${BASELINE_PASS} passed, ${BASELINE_FAIL} failed"
echo ""

# ── Process each class ──
DONE=0
FAILED=0

echo "$CANDIDATES" | while IFS='|' read -r CLASS API_COUNT AVG_SCORE SCENARIO SKILL_FILE; do
    echo "═══════════════════════════════════════════════════"
    echo "[$((DONE + 1))/${MAX_CLASSES}] ${CLASS}"
    echo "  APIs: ${API_COUNT} | Score: ${AVG_SCORE} | Scenario: ${SCENARIO}"
    echo "═══════════════════════════════════════════════════"

    # Load skill file content
    SKILL_PATH="${SKILLS_DIR}/${SKILL_FILE}"
    if [ -f "$SKILL_PATH" ]; then
        SKILL_CONTENT=$(cat "$SKILL_PATH")
    else
        SKILL_CONTENT="(No skill file found at ${SKILL_PATH})"
    fi

    # Build enriched prompt using Python
    PROMPT=$(python3 -c "
import sys; sys.path.insert(0, '${ORCH_ROOT}')
from shared.skill_loader import build_prompt
print(build_prompt(
    '${CLASS}', '${SCENARIO}', '${SKILL_FILE}',
    '${PROJECT_ROOT}', ${BASELINE_PASS}, ${BASELINE_FAIL}
))
")

    # Mark as claimed
    sqlite3 "${PROGRESS_DB}" "
        UPDATE tasks SET status='claimed', claimed_by='phase1',
                         claimed_at=datetime('now'), updated_at=datetime('now')
        WHERE android_class='${CLASS}';
    "

    echo "  Invoking Claude Code with skill-enriched prompt..."

    # Run Claude Code (non-interactive)
    CLAUDE_OUTPUT=$(claude --print "$PROMPT" 2>&1) || true

    # Verify: run tests ourselves
    echo "  Verifying tests..."
    cd "${PROJECT_ROOT}/test-apps"
    TEST_OUTPUT=$(./run-local-tests.sh 2>&1) || true
    PASS=$(echo "$TEST_OUTPUT" | grep -oP 'Passed:\s*\K\d+' | head -1 || echo "0")
    FAIL=$(echo "$TEST_OUTPUT" | grep -oP 'Failed:\s*\K\d+' | head -1 || echo "0")

    echo "  Results: ${PASS} passed, ${FAIL} failed (baseline: ${BASELINE_PASS}/${BASELINE_FAIL})"

    if [ "${FAIL}" -le "$((BASELINE_FAIL + 2))" ]; then
        sqlite3 "${PROGRESS_DB}" "
            UPDATE tasks SET status='tested_mock', test_pass=${PASS}, test_fail=${FAIL},
                             completed_at=datetime('now'), updated_at=datetime('now')
            WHERE android_class='${CLASS}';
            INSERT INTO task_log (task_id, android_class, action, worker_id, details)
            VALUES ((SELECT id FROM tasks WHERE android_class='${CLASS}'),
                    '${CLASS}', 'tested_mock', 'phase1', 'pass=${PASS} fail=${FAIL}');
        "
        echo "  ✓ ${CLASS} shimmed successfully"
        DONE=$((DONE + 1))
        BASELINE_PASS=$PASS
        BASELINE_FAIL=$FAIL
    else
        sqlite3 "${PROGRESS_DB}" "
            UPDATE tasks SET status='failed', test_pass=${PASS}, test_fail=${FAIL},
                             last_error='Test regression: ${FAIL} failures vs baseline ${BASELINE_FAIL}',
                             updated_at=datetime('now')
            WHERE android_class='${CLASS}';
            INSERT INTO task_log (task_id, android_class, action, worker_id, details)
            VALUES ((SELECT id FROM tasks WHERE android_class='${CLASS}'),
                    '${CLASS}', 'failed', 'phase1', 'regression: ${FAIL} > ${BASELINE_FAIL}+2');
        "
        echo "  ✗ ${CLASS} failed — test regression"
        FAILED=$((FAILED + 1))
        # Revert changes
        git -C "${PROJECT_ROOT}" checkout -- . 2>/dev/null || true
    fi

    echo ""
done

echo "═══ Phase 1 Complete ═══"
echo "Shimmed: ${DONE} | Failed: ${FAILED}"
echo ""
sqlite3 "${PROGRESS_DB}" "
    SELECT status, COUNT(*) as count, ROUND(AVG(avg_score),1) as avg
    FROM tasks GROUP BY status ORDER BY count DESC;
"
