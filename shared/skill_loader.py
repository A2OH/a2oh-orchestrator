"""
Load per-API skill files and build enriched prompts for AI code generation.
"""

import os
import sqlite3
from typing import Optional

SKILLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..',
                           'android-to-openharmony-migration', 'skills', 'per-api')
API_COMPAT_DB = os.path.join(os.path.dirname(__file__), '..', '..',
                              'android-to-openharmony-migration', 'database', 'api_compat.db')

SCENARIO_INSTRUCTIONS = {
    'S1': """SCENARIO: Direct Mapping (Thin Wrapper)
- Each method delegates to OHBridge.nativeXxx() — one bridge call per Android call
- Follow the pattern in shim/java/android/util/Log.java
- Test null args, boundary values, return types
- Expected: 1 iteration, >95% first-attempt success""",

    'S2': """SCENARIO: Signature Adaptation
- OH API has different parameter types/order — convert at the boundary
- Check Gap Descriptions for each method's specific conversion
- Map enums via switch/lookup, convert color ints to #AARRGGBB strings
- Test type edge cases: null, empty string, MAX/MIN, negative""",

    'S3': """SCENARIO: Partial Coverage
- Implement methods with score >= 5, stub the rest
- Every stub must: throw UnsupportedOperationException, return safe default, or log+no-op
- Comment each stub: // A2OH: not supported on OHOS
- Test both working methods AND verify stubs behave predictably""",

    'S4': """SCENARIO: Multi-API Composition
- One Android call may require multiple OH calls — create helper methods in OHBridge
- Map action strings, enum values, parameter structures
- Check Migration Guides for specific conversion patterns
- Test composition end-to-end: Android input → shim → bridge mock → verify""",

    'S5': """SCENARIO: Native Bridge Required
- Create Rust bridge module for JNI type marshalling
- Handle String↔char*, int↔jint, byte[]↔Vec<u8> carefully
- Mock returns plausible values; mark for Level 3 (QEMU) testing later
- Watch for: static field init from native, JNI thread safety""",

    'S6': """SCENARIO: UI Paradigm Shift (ViewTree)
- Do NOT create real UI. Build a ViewNode description tree.
- Each setter stores value in ViewNode.props map
- Containers manage ViewNode.children list
- Follow Property Mapping Table in AI Agent Playbook
- Test: addView/removeView, property propagation, event handler storage""",

    'S7': """SCENARIO: Async/Threading Gap
- Use Java concurrency: ExecutorService, BlockingQueue, CompletableFuture
- Handler: single-thread executor + message queue
- AsyncTask: thread pool + callbacks
- Add timeout to all blocking operations to prevent deadlock
- Test with concurrent calls to verify thread safety""",

    'S8': """SCENARIO: No Mapping (Stub Only)
- Minimal stub class matching AOSP package/class
- Lifecycle methods: no-op. Computation: throw. Queries: return default.
- Log warning on first use
- Only test: no crash on construction, expected exceptions""",
}


def load_skill_file(skill_filename: str) -> Optional[str]:
    """Load a per-API skill file content."""
    path = os.path.join(SKILLS_DIR, skill_filename)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return None


def get_api_details(android_class: str) -> str:
    """Query api_compat.db for all methods of a class."""
    db = sqlite3.connect(API_COMPAT_DB)
    db.row_factory = sqlite3.Row

    pkg = android_class.rsplit('.', 1)[0]
    cls = android_class.rsplit('.', 1)[1]

    rows = db.execute("""
        SELECT a.name, a.signature, a.kind,
               m.score, m.mapping_type, m.effort_level,
               m.gap_description, m.migration_guide,
               m.code_example_android, m.code_example_oh,
               oa.name AS oh_name, oa.signature AS oh_sig
        FROM api_mappings m
        JOIN android_apis a ON m.android_api_id = a.id
        JOIN android_types t ON a.type_id = t.id
        JOIN android_packages p ON t.package_id = p.id
        LEFT JOIN oh_apis oa ON m.oh_api_id = oa.id
        WHERE p.name = ? AND t.full_name = ?
          AND a.kind IN ('method','constructor')
        ORDER BY m.score DESC
    """, (pkg, cls)).fetchall()
    db.close()

    if not rows:
        return "(No API details found in database)"

    lines = ["| Method | Signature | Score | Type | OH Equivalent |",
             "|---|---|---|---|---|"]
    for r in rows:
        oh = r['oh_name'] or '—'
        sig = (r['signature'] or '')[:60]
        lines.append(f"| {r['name']} | {sig} | {r['score']:.0f} | {r['mapping_type']} | {oh} |")

    # Add gap descriptions
    gaps = [(r['name'], r['gap_description']) for r in rows if r['gap_description']]
    if gaps:
        lines.append("")
        lines.append("GAP DESCRIPTIONS:")
        for name, gap in gaps[:10]:
            lines.append(f"  {name}: {gap}")

    # Add migration guides
    guides = [(r['name'], r['migration_guide']) for r in rows if r['migration_guide']]
    if guides:
        lines.append("")
        lines.append("MIGRATION GUIDES:")
        for name, guide in guides[:10]:
            lines.append(f"  {name}: {guide}")

    # Add code examples
    examples = [(r['name'], r['code_example_android'], r['code_example_oh'])
                for r in rows if r['code_example_android'] or r['code_example_oh']]
    if examples:
        lines.append("")
        lines.append("CODE EXAMPLES:")
        for name, android_ex, oh_ex in examples[:5]:
            if android_ex:
                lines.append(f"  Android {name}: {android_ex[:200]}")
            if oh_ex:
                lines.append(f"  OH {name}: {oh_ex[:200]}")

    return "\n".join(lines)


def build_prompt(android_class: str, scenario: str, skill_filename: str,
                 project_root: str, baseline_pass: int = 0, baseline_fail: int = 0,
                 previous_error: str = None) -> str:
    """
    Build the complete prompt for an AI agent to shim one Android class.
    """
    skill_content = load_skill_file(skill_filename) or "(Skill file not found)"
    api_details = get_api_details(android_class)
    scenario_instr = SCENARIO_INSTRUCTIONS.get(scenario, SCENARIO_INSTRUCTIONS['S3'])
    class_path = android_class.replace('.', '/')

    prompt = f"""You are working on the Android→OpenHarmony shim layer in {project_root}.

TARGET CLASS: {android_class}

{scenario_instr}

== PER-API SKILL FILE ==
{skill_content}

== API DETAILS FROM DATABASE ==
{api_details}

== TASK ==
1. Create/update Java shim class at shim/java/{class_path}.java
   - Match AOSP package and class name exactly
   - Follow scenario instructions above
   - For implementable APIs (score >= 5): implement with OHBridge delegation
   - For stub APIs (score < 5): follow stub strategy from skill file

2. Add new OHBridge native declarations to:
   shim/java/com/ohos/shim/bridge/OHBridge.java
   (append only — do not modify existing methods)

3. Add mock implementations to:
   test-apps/mock/com/ohos/shim/bridge/OHBridge.java
   (append only — do not modify existing mocks)

4. Add test section to:
   test-apps/02-headless-cli/src/HeadlessTest.java
   (add new test method, do not modify existing tests)

5. Compile and run tests:
   cd {project_root}/test-apps && ./run-local-tests.sh
   Fix any compilation errors or test failures.

== CONSTRAINTS ==
- Do not break existing tests (baseline: {baseline_pass} pass, {baseline_fail} fail)
- Keep shim minimal — only implement what's needed
- Do NOT commit — just make code compile and tests pass
"""

    if previous_error:
        prompt += f"""
== PREVIOUS ATTEMPT FAILED ==
{previous_error}

Fix ONLY the specific error above. Do not rewrite the entire class.
Show the minimal diff needed.
"""

    return prompt
