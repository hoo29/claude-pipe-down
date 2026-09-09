"""Benchmark the regex path of the hook on representative edits, judge excluded.

Run: PIPE_DOWN_LLM=0 python3 tests/bench_check_comments.py
"""

import functools
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
os.environ["PIPE_DOWN_LLM"] = "0"
os.environ.setdefault("CLAUDE_PLUGIN_DATA", tempfile.mkdtemp())

import check_comments as cc

SCRIPT = os.path.join(ROOT, "hooks", "check_comments.py")

TS_UNIT = """\
// Payload must be valid before it reaches the queue
export function handle{n}(payload: Payload): Result {{
  const parsed = parse(payload); // trailing note {n}
  if (!parsed.ok) {{
    return fail(parsed.error);
  }}
  return enqueue(parsed.value, {{ retries: {n} % 3 }});
}}

/**
 * Sends the result downstream.
 * @param r result to send
 */
export function send{n}(r: Result): void {{
  emit(r);
}}
"""

PY_UNIT = '''\
def handle_{n}(payload):
    """Validate payload before it reaches the queue."""
    parsed = parse(payload)  # trailing note {n}
    if not parsed.ok:
        return fail(parsed.error)
    # Retries are bounded by the caller's deadline
    return enqueue(parsed.value, retries={n} % 3)


def send_{n}(result):
    emit(result)
'''

JAVA_TEST_UNIT = """\
    @Test
    void handles{n}() {{
        // Given
        var cart = new Cart();
        // When
        cart.add(item{n});
        // Then
        assertEquals({n}, cart.size());
    }}
"""


def make_file(unit: str, lines: int) -> str:
    out: List[str] = []
    n = 0
    while sum(s.count("\n") for s in out) < lines:
        out.append(unit.format(n=n))
        n += 1
    return "".join(out)


def event(tool: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    return {"session_id": "bench", "tool_name": tool, "tool_input": tool_input, "hook_event_name": "PreToolUse"}


def run_in_process(ev: Dict[str, Any]) -> str:
    """Run the hook once on ev without a subprocess and return its stdout."""
    saved = sys.stdin, sys.stdout, sys.stderr, cc.USE_LLM
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(json.dumps(ev)), io.StringIO(), io.StringIO()
    cc.USE_LLM = False
    try:
        cc.main()
        return sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.stderr, cc.USE_LLM = saved


def timeit(fn: Callable[[], object], repeat: int) -> float:
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples) * 1000


def scenarios(tmp: str) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for lines in (50, 500, 5000):
        ts = make_file(TS_UNIT, lines)
        py = make_file(PY_UNIT, lines)
        ts_path = os.path.join(tmp, f"write_{lines}.ts")
        with open(ts_path, "w") as fh:
            fh.write(ts)
        new_ts = {"file_path": os.path.join(tmp, f"new_{lines}.ts"), "content": ts}
        new_py = {"file_path": os.path.join(tmp, f"new_{lines}.py"), "content": py}
        over_ts = {"file_path": ts_path, "content": ts.replace("note 1", "changed 1")}
        snippet = TS_UNIT.format(n=7)
        edit = {"file_path": ts_path, "old_string": snippet, "new_string": snippet.replace("note 7", "why 7")}
        edits = [
            {"old_string": TS_UNIT.format(n=k), "new_string": TS_UNIT.format(n=k).replace(f"note {k}", f"why {k}")}
            for k in range(1, 6)
        ]
        out.append((f"Write new {lines}-line ts", event("Write", new_ts)))
        out.append((f"Write new {lines}-line py", event("Write", new_py)))
        out.append((f"Write over {lines}-line ts", event("Write", over_ts)))
        out.append((f"Edit 16 lines in {lines}-line ts", event("Edit", edit)))
        out.append((f"MultiEdit 5 in {lines}-line ts", event("MultiEdit", {"file_path": ts_path, "edits": edits})))
    narrative = make_file(TS_UNIT.replace("Payload must be valid", "Validate the payload"), 500)
    out.append(
        (
            "Write denied 500-line ts",
            event("Write", {"file_path": os.path.join(tmp, "denied.ts"), "content": narrative}),
        )
    )
    java = "class CartTest {\n" + make_file(JAVA_TEST_UNIT, 400) + "}\n"
    java_path = os.path.join(tmp, "src", "test", "CartTest.java")
    out.append(("Write 400-line java test, bdd", event("Write", {"file_path": java_path, "content": java})))
    return out


def subprocess_ms(argv: List[str], stdin: str, repeat: int) -> float:
    env = dict(os.environ)

    def run() -> None:
        subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env)

    return timeit(run, repeat)


def main() -> None:
    repeat = int(os.environ.get("BENCH_REPEAT", "20"))
    tmp = tempfile.mkdtemp()
    rows = []
    for name, ev in scenarios(tmp):
        decision = "deny" if run_in_process(ev) else "allow"
        ms = timeit(functools.partial(run_in_process, ev), repeat)
        rows.append((name, decision, ms))
    width = max(len(r[0]) for r in rows)
    print(f"{'scenario':<{width}}  result  check ms")
    for name, decision, ms in rows:
        print(f"{name:<{width}}  {decision:<6}  {ms:8.2f}")

    small = scenarios(tmp)[0][1]
    startup = subprocess_ms([sys.executable, "-c", "pass"], "", repeat)
    imports = subprocess_ms([sys.executable, "-c", "import json, os, re, shlex, subprocess, sys, time"], "", repeat)
    module = subprocess_ms(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {ROOT + '/hooks'!r}); import check_comments"],
        "",
        repeat,
    )
    hook = subprocess_ms([sys.executable, SCRIPT], json.dumps(small), repeat)
    print()
    print("process wall time, median ms")
    print(f"python startup                 {startup:8.2f}")
    print(f"startup + stdlib imports       {imports:8.2f}")
    print(f"startup + hook module import   {module:8.2f}")
    print(f"full hook, 50-line write       {hook:8.2f}")


if __name__ == "__main__":
    main()
