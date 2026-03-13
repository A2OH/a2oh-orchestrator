# A2OH Orchestrator: Massive-Scale Android API Porting

## Problem

We have 679 Android API classes to port to OpenHarmony via AI-generated shim code. Each class needs: prompt construction → code generation → compilation → testing → retry on failure → commit. Running this sequentially in one Claude Code session would take days. We need massive parallelism.

## Two Approaches Compared

### Option A: Internal Subagents (Single Claude Code Session)

```
┌─────────────────────────────────┐
│  Claude Code (1 session)        │
│  ┌────────┐ ┌────────┐         │
│  │SubAg 1 │ │SubAg 2 │ ...     │
│  │Handler │ │Looper  │         │
│  └────────┘ └────────┘         │
│  Same context, same rate limit  │
│  Rich inter-agent communication │
└─────────────────────────────────┘
```

### Option B: External Orchestrator (N Independent Claude Code / API Instances)

```
┌──────────────────────┐
│  Python Dispatcher    │
│  (shim_progress.db)   │
└──────┬───────────────┘
       │ spawns N workers
 ┌─────┼─────┬─────┐
 ▼     ▼     ▼     ▼
W #1  W #2  W #3  W #N
(isolated working dirs)
```

### Comparison Matrix

| Dimension | Internal Subagents | External Orchestrator |
|---|---|---|
| **Throughput** | Bound to 1 API key (~50 req/min). N subagents compete for same pipe. 679 classes = days. | N API keys × 50 req/min. 50 parallel workers = hours. **Winner.** |
| **Agent Capabilities** | Full CC toolkit: Read/Edit/Grep/Glob, sandboxed Bash, git awareness, memory, hooks, CLAUDE.md. Subagents inherit parent context. **Winner.** | Same tools in danger mode, but no shared context, no parent-child relationship, no resume, no inter-agent communication. |
| **Failure Handling** | Parent sees failures directly, can make intelligent retry decisions. But if parent session crashes, everything is lost. No checkpointing. | Orchestrator checkpoints per task. Each worker isolated — one crash doesn't affect others. Failed tasks re-queued. **Winner.** |
| **Git Conflicts** | All subagents share filesystem — race conditions on shared files (OHBridge.java, HeadlessTest.java). Must serialize or lock. | Same problem but solvable: git worktrees, isolated dirs, per-class bridge files. |
| **Cost** | "Free" with Max subscription but limited by rate. | API: ~$170 for all 679 classes. 5 Max accounts: $1,000/mo. API is cheaper for batch. |
| **Observability** | One terminal, hard to track 679 tasks. No dashboard. | Task table with status tracking, logs per task, easy to build dashboard. **Winner.** |
| **Dev Effort** | Minimal — enhance existing loop script. ~1-2 hours. **Winner for Phase 1.** | Python dispatcher + file isolation + merge step. ~1-2 days. |
| **Scalability** | ~5-10 parallel subagents max before rate limits. | Unlimited horizontal scaling with more API keys. **Winner.** |

### Key Insight: Shared File Problem

Both approaches must solve the same bottleneck: `OHBridge.java` and `HeadlessTest.java` are modified by every task.

**Solution: Per-class file isolation.**
- Instead of one `OHBridge.java`, each class gets: `OHBridgeExt_Handler.java`, `OHBridgeExt_MediaPlayer.java`
- Instead of one `HeadlessTest.java`, each class gets: `Test_Handler.java`, `Test_MediaPlayer.java`
- A master `OHBridge.java` aggregates all extensions
- A master test runner discovers all `Test_*.java` files
- Workers never touch the same file → zero merge conflicts

## Decision: Hybrid Approach

**Phase 1: Internal subagents** — validate the pipeline on 5-10 classes, debug prompt quality, fix skill file gaps. Zero infrastructure cost.

**Phase 2: External orchestrator** — custom Python dispatcher (not full Attractor — our pipeline is linear enough). Claude API for cost efficiency. 50+ parallel workers. Hours not days.

---

## Architecture

```
a2oh-orchestrator/
├── ARCHITECTURE.md              ← this file
├── phase1/                      ← Internal subagent approach
│   └── enhanced-loop.sh         ← Skill-enriched a2oh-loop.sh
├── phase2/                      ← External orchestrator
│   ├── dispatcher.py            ← Master: query DB → spawn workers
│   ├── worker.py                ← Per-task: prompt → generate → compile → test
│   ├── merge.py                 ← Combine isolated results into main repo
│   ├── dashboard.py             ← Progress monitoring
│   └── templates/
│       └── prompt_template.py   ← Prompt builder from skill files + DB
├── shared/
│   ├── db.py                    ← shim_progress.db operations (claim/update/query)
│   ├── skill_loader.py          ← Read per-API skill file into prompt context
│   └── file_isolation.py        ← Per-class bridge/test file generation
├── config.yaml                  ← API keys, parallelism, paths
└── pipelines/
    └── api-shim.dot             ← Attractor-compatible DOT (for reference/future use)
```

### Data Flow

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│api_compat.db │────▶│ dispatcher.py│────▶│  N × worker.py  │
│(57,289 APIs) │     │              │     │  (parallel)     │
└─────────────┘     │ Builds queue │     │                 │
                    │ Claims tasks │     │ 1. Load skill   │
┌─────────────┐     │ Monitors     │     │ 2. Build prompt │
│skills/per-api│────▶│              │     │ 3. Call Claude  │
│(679 files)  │     └──────┬───────┘     │ 4. Compile      │
└─────────────┘            │             │ 5. Test         │
                           │             │ 6. Retry ≤3     │
┌──────────────┐           │             └────────┬────────┘
│shim_progress │◀──────────┘                      │
│    .db       │◀─────────────────────────────────┘
│(task states) │
└──────────────┘
        │
        ▼
┌──────────────┐     ┌─────────────┐
│  merge.py    │────▶│ main repo   │
│(combine all) │     │ git commit  │
└──────────────┘     └─────────────┘
```

### Task State Machine

```
pending ──claim──▶ claimed ──success──▶ tested_mock
                      │                      │
                      │ compile_fail         │ (Phase 2+)
                      │ test_fail            ▼
                      ▼                 tested_device
                   retrying                  │
                      │                      ▼
                      │ max_retries      verified
                      ▼
                   failed ──human──▶ pending (re-queue)
```
