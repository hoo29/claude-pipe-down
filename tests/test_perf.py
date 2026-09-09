"""Timing guards for the regex path. Bounds are loose so slow CI runners do not fail them."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PIPE_DOWN_LLM"] = "0"
os.environ.setdefault("CLAUDE_PLUGIN_DATA", tempfile.mkdtemp())

import bench_check_comments as bench

REPEAT = 5


def best_ms(fn):
    """Return the fastest of REPEAT runs, which is the least sensitive to runner noise."""
    samples = []
    for _ in range(REPEAT):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return min(samples) * 1000


class PerfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.by_name = dict(bench.scenarios(cls.tmp))

    def check_ms(self, name):
        ev = self.by_name[name]
        return best_ms(lambda: bench.run_in_process(ev))

    def test_edit_is_independent_of_file_size(self):
        small = self.check_ms("Edit 16 lines in 50-line ts")
        large = self.check_ms("Edit 16 lines in 5000-line ts")
        self.assertLess(large, 20, f"edit into a 5000-line file took {large:.1f} ms")
        self.assertLess(large, max(small * 5, 5), f"edit cost grew with file size: {small:.2f} -> {large:.2f} ms")

    def test_multiedit_stays_cheap(self):
        ms = self.check_ms("MultiEdit 5 in 5000-line ts")
        self.assertLess(ms, 40, f"5 edits into a 5000-line file took {ms:.1f} ms")

    def test_write_scales_linearly(self):
        mid = self.check_ms("Write new 500-line ts")
        large = self.check_ms("Write new 5000-line ts")
        self.assertLess(large, 1000, f"5000-line write took {large:.1f} ms")
        self.assertLess(large, max(mid * 30, 300), f"write cost is superlinear: {mid:.1f} -> {large:.1f} ms")

    def test_denied_write_costs_no_more_than_allowed(self):
        allowed = self.check_ms("Write new 500-line ts")
        denied = self.check_ms("Write denied 500-line ts")
        self.assertLess(denied, max(allowed * 3, 50), f"deny path {denied:.1f} ms vs allow {allowed:.1f} ms")

    def test_bdd_test_file_write(self):
        ms = self.check_ms("Write 400-line java test, bdd")
        self.assertLess(ms, 200, f"400-line BDD test file took {ms:.1f} ms")

    def test_process_overhead(self):
        env = dict(os.environ)
        small = self.by_name["Write new 50-line ts"]
        payload = bench.json.dumps(small)

        def run():
            subprocess.run([sys.executable, bench.SCRIPT], input=payload, capture_output=True, text=True, env=env)

        def startup():
            subprocess.run([sys.executable, "-c", "pass"], capture_output=True, text=True, env=env)

        base = best_ms(startup)
        hook = best_ms(run)
        self.assertLess(hook - base, 500, f"hook adds {hook - base:.1f} ms over a bare interpreter ({base:.1f} ms)")


if __name__ == "__main__":
    unittest.main()
