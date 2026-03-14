# A2OH Orchestrator

Distributed orchestration for the [Android-to-OpenHarmony migration](https://github.com/A2OH/harmony-android-guide). Manages parallel Claude Code workers that claim GitHub Issues, implement Android API shims, verify tests, and close issues automatically.

```
                        ┌────────────────────────┐
                        │   GitHub Issues (queue) │
                        │   396 shim tasks        │
                        │   todo / in-progress /  │
                        │   done / failed         │
                        └───────┬────────────────┘
                                │
               ┌────────────────┼────────────────┐
               │                │                │
        ┌──────▼──────┐  ┌─────▼───────┐  ┌─────▼───────┐
        │  Worker #1  │  │  Worker #2  │  │  Worker #3  │
        │ a2oh-worker │  │ a2oh-worker │  │ a2oh-worker │
        │   .sh       │  │   .sh       │  │   .sh       │
        │             │  │             │  │             │
        │ ┌─────────┐ │  │ ┌─────────┐ │  │ ┌─────────┐ │
        │ │CC Agent │ │  │ │CC Agent │ │  │ │CC Agent │ │
        │ │5 parallel│ │  │ │5 parallel│ │  │ │5 parallel│ │
        │ │subagents │ │  │ │subagents │ │  │ │subagents │ │
        │ └─────────┘ │  │ └─────────┘ │  │ └─────────┘ │
        └─────────────┘  └─────────────┘  └─────────────┘
              │                │                │
              └────────────────┼────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │ harmony-android-    │
                    │ guide (main repo)   │
                    │ git push per batch  │
                    └─────────────────────┘
```

## Quick Start

### 1. Launch a Single Worker

```bash
./a2oh-worker.sh /tmp/worker1
```

This will:
1. Clone `A2OH/harmony-android-guide` to `/tmp/worker1` (or pull latest if it exists)
2. Verify prerequisites (javac, gh auth, claude CLI)
3. Run test baseline (expects 497/502 pass)
4. Launch Claude Code in autonomous mode to claim and implement 10 issues

### 2. Launch Multiple Workers in Parallel

```bash
./a2oh-worker.sh /tmp/worker1 10 &
./a2oh-worker.sh /tmp/worker2 10 &
./a2oh-worker.sh /tmp/worker3 10 &
```

Each worker needs its own directory (separate git clone) to avoid conflicts.

### 3. Loop Mode (Keep Going Until Done)

```bash
LOOP=1 ./a2oh-worker.sh /tmp/worker1
```

Keeps claiming issues and re-launching CC until no `todo` issues remain for the given tier.

---

## a2oh-worker.sh Reference

```
Usage: ./a2oh-worker.sh [WORKER_DIR] [BATCH_SIZE]

Arguments:
  WORKER_DIR   Directory for the git clone (default: /tmp/a2oh-worker-<PID>)
  BATCH_SIZE   Number of issues to claim per CC session (default: 10)

Environment variables:
  TIER         Which tier to work on: a, b, c, d (default: a)
  LOOP         Set to 1 for continuous mode (default: 0)
```

### Examples

```bash
# Basic — claim 10 Tier A issues, implement, close
./a2oh-worker.sh /tmp/worker1

# Custom batch size
./a2oh-worker.sh /tmp/worker1 5

# Work on Tier B issues
TIER=b ./a2oh-worker.sh /tmp/worker1

# Tier C, loop mode, batch of 5
TIER=c LOOP=1 ./a2oh-worker.sh /tmp/worker1 5

# Three parallel workers on Tier A
./a2oh-worker.sh /tmp/w1 10 &
./a2oh-worker.sh /tmp/w2 10 &
./a2oh-worker.sh /tmp/w3 10 &
```

### What the Script Does

```
Step 1: CLONE / UPDATE
  ├── New directory → git clone harmony-android-guide
  └── Existing directory → git fetch + git reset --hard origin/main
       (handles force-push / rewritten history gracefully)

Step 2: VERIFY PREREQUISITES
  ├── javac (JDK 8+)
  ├── gh auth status
  ├── Test infrastructure files exist
  └── claude CLI installed

Step 3: RUN TEST BASELINE
  └── test-apps/run-local-tests.sh headless → expects 497/502 pass

Step 4: LAUNCH CLAUDE CODE
  └── claude --dangerously-skip-permissions -p "<prompt>"
       ├── Claims up to BATCH_SIZE issues (gh issue edit --remove-label todo --add-label in-progress)
       ├── Launches parallel subagents (one per shim class)
       ├── Each agent: reads stub → reads skill file → implements real Java logic
       ├── Verifies test baseline holds
       ├── git add + git commit + git push
       └── gh issue close + relabel done

Step 5: LOOP (if LOOP=1)
  ├── Checks for remaining todo issues
  ├── git fetch + reset to latest main
  ├── Re-launches CC
  └── Stops when no todo issues left
```

### Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| Java JDK | 8+ (21 works) | `javac -version` |
| GitHub CLI | Any | `gh auth status` |
| Claude Code | Latest | `claude --version` |
| Git | Any | `git --version` |

Install Claude Code:
```bash
npm install -g @anthropic-ai/claude-code
```

---

## Running Without the Script

If you prefer to invoke CC directly:

```bash
cd /path/to/harmony-android-guide

claude --dangerously-skip-permissions -p "
Read CLAUDE.md. Claim up to 10 open tier-a todo issues from A2OH/harmony-android-guide.
Implement each shim with real Java logic, verify 497/502 baseline holds, close as done.
Use parallel agents (5 at a time).
Do NOT add Co-Authored-By lines to commits."
```

---

## Throughput Estimates

Each CC session runs up to 5 parallel subagents. Each agent implements one shim class.

| Setup | Concurrent Agents | Approx. Rate |
|-------|-------------------|--------------|
| 1 CC session | 5 | ~100 classes/hour |
| 3 CC sessions | 15 | ~300 classes/hour |
| 5 CC sessions | 25 | ~500 classes/hour |

```
Tier A:  314 classes →  ~1 hour with 3 workers
Tier B:  946 classes →  ~3 hours with 3 workers
Tier C: 3,445 classes → ~12 hours with 3 workers (needs OHBridge)
Tier D:  613 classes →  ~2 hours with 3 workers (needs ArkUI)
Total: 5,318 classes / 57,289 APIs
```

---

## API Tier Breakdown

| Tier | Classes | APIs | Dependency | Strategy |
|------|---------|------|------------|----------|
| **A** | 314 | 1,316 | None (pure Java) | HashMap, ArrayList, standard Java — no JNI |
| **B** | 946 | 2,212 | File system | Java I/O fallback (e.g. SharedPreferences → HashMap + file) |
| **C** | 3,445 | 43,254 | OHBridge (JNI) | Routes to OHOS native APIs via JNI bridge |
| **D** | 613 | 10,507 | ArkUI | UI rewrite from Android Views to ArkUI components |

Priority: A first, then B, then C. D is deferred.

---

## Monitoring Progress

### Command Line

```bash
# Issue counts by status
for label in done in-progress todo failed; do
  count=$(gh issue list --repo A2OH/harmony-android-guide --label $label \
    --json number 2>/dev/null | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
  echo "$count $label"
done

# Watch in-progress count
watch -n 10 'gh issue list --repo A2OH/harmony-android-guide \
  --label in-progress --json number \
  | python3 -c "import json,sys; print(len(json.load(sys.stdin)),\"in-progress\")"'
```

### Orchestrator Dashboard

The React dashboard at GitHub Pages shows real-time issue status, progress bars, per-tier stats, and issue management actions (Claim/Done/Fail/Release/Reopen).

Deployed from `A2OH/harmony-android-guide` → `frontend/`.

---

## Python Orchestrator (Advanced)

For more control, the `phase2/dispatcher.py` provides a Python-based orchestrator that:
- Manages its own SQLite progress DB (`shim_progress.db`)
- Spawns N parallel workers via Claude API or Claude Code CLI
- Handles retries, claim timeouts, and file isolation
- Per-class bridge/test/mock file isolation to prevent merge conflicts

```bash
# Configure
vim config.yaml

# Run dispatcher
python3 phase2/dispatcher.py --max-workers 10 --tier a
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

---

## Repository Structure

```
a2oh-orchestrator/
├── README.md              ← This file
├── ARCHITECTURE.md        ← Design doc: subagents vs external orchestration
├── config.yaml            ← Paths, API config, isolation strategy
├── a2oh-worker.sh         ← CC worker launcher (clone + verify + launch)
├── phase1/
│   └── enhanced_loop.sh   ← Single-session subagent loop
├── phase2/
│   ├── dispatcher.py      ← Python orchestrator (spawns N workers)
│   ├── dashboard.py       ← Terminal dashboard
│   └── merge.py           ← Auto-merge completed branches
├── pipelines/
│   └── api-shim.dot       ← Pipeline visualization
└── shared/
    ├── db.py              ← Database helpers
    └── skill_loader.py    ← Skill file lookup
```

## Related Repos

| Repo | Purpose |
|------|---------|
| [A2OH/harmony-android-guide](https://github.com/A2OH/harmony-android-guide) | Main repo: shims, tests, DB, frontend |
| [A2OH/a2oh-orchestrator](https://github.com/A2OH/a2oh-orchestrator) | This repo: worker scripts, dispatcher |
