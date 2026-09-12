import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import check_comments as cc

SCRIPT = os.path.join(ROOT, "hooks", "check_comments.py")
STATE_DIR = tempfile.mkdtemp()

FILLER = "This function is responsible for"
FILLER_WORDS = len(FILLER.split())


def padding(n):
    """Return n neutral words that trip no rule on their own."""
    return " ".join("word" for _ in range(n))


def run_hook(tool, tool_input, session="test", env_extra=None):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = STATE_DIR
    env["PIPE_DOWN_LLM"] = "0"
    if env_extra:
        env.update(env_extra)
    event = {"session_id": session, "tool_name": tool, "tool_input": tool_input, "hook_event_name": "PreToolUse"}
    proc = subprocess.run([sys.executable, SCRIPT], input=json.dumps(event), capture_output=True, text=True, env=env)
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]


def deny_reason(tool, tool_input, session="test", env_extra=None):
    out = run_hook(tool, tool_input, session, env_extra)
    assert out is not None, f"expected {tool} to be denied, hook allowed it: input={tool_input!r}"
    assert out["permissionDecision"] == "deny", f"expected deny, got {out['permissionDecision']!r}: {out!r}"
    return out["permissionDecisionReason"]


def problems(text, lang):
    comments = cc.extract_comments(text, lang)
    assert comments, f"no comment extracted from {text!r}"
    return [rule for rule, _ in cc.find_problems(comments[0])]


class ExtractionTests(unittest.TestCase):
    def test_line_and_trailing(self):
        text = "// top\nconst a = 1; // trailing\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual(
            [(c.text, c.trailing) for c in cs],
            [("top", False), ("trailing", True)],
            "expected one leading and one trailing comment",
        )
        self.assertEqual(cs[0].next_code, "const a = 1; // trailing", "next_code of the leading comment")
        self.assertEqual(cs[1].same_line_code, "const a = 1;", "same_line_code of the trailing comment")

    def test_marker_inside_string_ignored(self):
        text = 'const url = "http://example.com"; // real\n'
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual([c.text for c in cs], ["real"], "// inside a string literal must not start a comment")

    def test_hash_in_shell_expansion_ignored(self):
        text = 'echo "${#arr[@]}" $# # count\n'
        cs = cc.extract_comments(text, cc.HASH)
        self.assertEqual([c.text for c in cs], ["count"], "# inside shell expansions must not start a comment")

    def test_consecutive_lines_merge(self):
        text = "# one\n# two\nx = 1\n"
        cs = cc.extract_comments(text, cc.PYTHON)
        self.assertEqual(len(cs), 1, f"adjacent comment lines should merge into one comment, got {cs!r}")
        self.assertEqual(cs[0].line_count, 2, "merged comment should report both lines")
        self.assertEqual(cs[0].text, "one two", "merged comment text should join the lines")

    def test_jsdoc_is_doc(self):
        text = "/**\n * Summary.\n * @param {string} x name\n */\nfunction f(x) {}\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual(cs[0].kind, "doc", "/** block before a function should be a doc comment")
        self.assertEqual(cs[0].next_code, "function f(x) {}", "next_code should skip the block closer")

    def test_python_docstring_is_doc(self):
        text = 'def f():\n    """Summary."""\n    return 1\n'
        cs = cc.extract_comments(text, cc.PYTHON)
        self.assertEqual(cs[0].kind, "doc", "docstring should be a doc comment")
        self.assertEqual(cs[0].text, "Summary.", "docstring text should exclude the quotes")

    def test_go_decl_comment_is_doc(self):
        text = "// Parse reads the config.\nfunc Parse() {}\n"
        self.assertEqual(cc.extract_comments(text, cc.GO)[0].kind, "doc", "Go comment before func should be doc")
        self.assertEqual(
            cc.extract_comments("# Usage: run.sh <dir>\nrun() {\n}\n", cc.SHELL)[0].kind,
            "doc",
            "shell comment before a function should be doc",
        )

    def test_js_line_comment_before_function_is_not_doc(self):
        text = "// Parse reads the config.\nfunction parse() {}\n"
        self.assertEqual(cc.extract_comments(text, cc.C_LIKE)[0].kind, "line", "JS // before function is not doc")

    def test_comment_before_local_const_is_not_doc(self):
        text = "function f() {\n  // Read the file\n  const raw = read();\n}\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual(cs[0].kind, "line", "comment before a local const is not doc")

    def test_rust_doc_line(self):
        text = "/// Parses input.\npub fn parse() {}\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual(cs[0].kind, "doc", "/// should be a doc comment")

    def test_lang_lookup(self):
        for path, lang in [
            ("/x/Dockerfile", cc.HASH),
            ("/x/main.go", cc.GO),
            ("/x/run.sh", cc.SHELL),
            ("/x/a.tsx", cc.C_LIKE),
            ("/x/README.md", None),
            ("/x/.gitignore", cc.HASH),
            ("/x/.env", cc.HASH),
            ("/x/.editorconfig", cc.INI),
        ]:
            self.assertIs(cc.lang_for(path), lang, f"lang_for({path!r})")

    def test_multiline_string_body_is_not_a_comment(self):
        for text, lang in [
            ('HELP = """\n# Imports\nfoo\n"""\nx = 1  # real\n', cc.PYTHON),
            ("const s = `\n// Load the config\n`; // real\nx();\n", cc.C_LIKE),
            ("var t = `\n// Helpers\n`\nx := 1 // real\n", cc.GO),
            ('val s = """\n// Helpers\n"""\nx() // real\n', cc.C_LIKE),
        ]:
            cs = cc.extract_comments(text, lang)
            self.assertEqual([c.text for c in cs], ["real"], f"string body must not be a comment: {text!r}")

    def test_unterminated_single_quote_does_not_carry_over(self):
        text = "fn f<'a>() {}\n// real\nx();\n"
        self.assertEqual([c.text for c in cc.extract_comments(text, cc.C_LIKE)], ["real"])

    def test_empty_doc_block_closes(self):
        text = "/**/\nconst a = 1;\n// Helpers\nfunction x() {}\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual([(c.kind, c.text) for c in cs], [("doc", ""), ("line", "Helpers")], f"got {cs!r}")
        self.assertEqual(cs[0].end, 0, "/**/ must close on its own line")

    def test_next_code_skips_block_comment_body(self):
        text = "// Build the request\n/**\n * Request builder\n */\nfunction f() {}\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual(cs[0].next_code, "function f() {}", "next_code must not be a block comment line")
        cs = cc.extract_comments('# note\n"""\nfunc helper() {\n"""\nreturn x\n', cc.PYTHON)
        self.assertEqual(cs[0].next_code, "return x", "next_code must skip a docstring body")

    def test_comment_after_shebang_is_kept(self):
        text = "#!/usr/bin/env python\n# Import the modules\nimport os\n"
        cs = cc.extract_comments(text, cc.PYTHON)
        self.assertEqual([c.text for c in cs], ["Import the modules"], "shebang dropped, next comment kept")
        self.assertEqual(cs[0].raw, ["# Import the modules"], "raw should not include the shebang")
        self.assertFalse(cc.is_exempt(cs[0]), "comment after a shebang must not inherit the shebang exemption")
        self.assertEqual(cc.extract_comments("#!/bin/sh\necho hi\n", cc.SHELL), [], "shebang alone is not a comment")


class RuleTests(unittest.TestCase):
    def assertRule(self, rule, text, lang):
        p = problems(text, lang)
        self.assertIn(rule, p, f"expected {rule!r} for {text!r}, got {p!r}")

    def assertNoRule(self, rule, text, lang):
        p = problems(text, lang)
        self.assertNotIn(rule, p, f"did not expect {rule!r} for {text!r}, got {p!r}")

    def assertClean(self, text, lang):
        p = problems(text, lang)
        self.assertEqual(p, [], f"expected no problems for {text!r}, got {p!r}")

    def test_history(self):
        self.assertRule("history", "// Changed to use async instead of sync\nawait f();\n", cc.C_LIKE)
        self.assertRule("history", "# Previously this returned a list\nreturn x\n", cc.PYTHON)
        self.assertRule("history", "# No longer needed after the refactor\nreturn x\n", cc.PYTHON)
        self.assertRule("history", "// Used to be synchronous\nawait f();\n", cc.C_LIKE)
        self.assertRule("history", "// Leftover from the old parser\nf();\n", cc.C_LIKE)
        for text in [
            "// Used to detect cycles in the graph\nf();\n",
            "// Leftover bytes are padding\nf();\n",
            "// No longer valid after expiry\nf();\n",
            "// Extracted from the JWT header\nf();\n",
            "// Restored from the snapshot on boot\nf();\n",
        ]:
            self.assertNoRule("history", text, cc.C_LIKE)

    def test_restate(self):
        self.assertRule("restate", "// Parse and return the JSON\nreturn JSON.parse(raw);\n", cc.C_LIKE)
        self.assertRule("restate", "# Set the port\nport: 80\n", cc.HASH)
        self.assertRule("restate", "x = load_users()  # load users\n", cc.PYTHON)

    def test_narrative(self):
        self.assertRule("narrative", "// Import the required modules\nimport fs from 'fs';\n", cc.C_LIKE)
        self.assertRule("narrative", "// Now we iterate over each entry\nfor (const e of xs) {}\n", cc.C_LIKE)
        self.assertRule("narrative", "// Push the item onto the stack\nf();\n", cc.C_LIKE)
        self.assertRule("narrative", "// Hash the password\nf();\n", cc.C_LIKE)
        for text in [
            "// Format: <major>.<minor>\nf();\n",
            "// Sign bit lives in the top byte\nf();\n",
            "// Stop words are dropped\nf();\n",
            "// Hash of the parent block\nf();\n",
        ]:
            self.assertNoRule("narrative", text, cc.C_LIKE)

    def test_label_and_banner(self):
        self.assertRule("label", "// Helpers\nfunction a() {}\n", cc.C_LIKE)
        self.assertRule("label", "# Imports\nimport os\n", cc.PYTHON)
        self.assertRule("banner", "// ------ Setup ------\nlet a;\n", cc.C_LIKE)
        self.assertRule("banner", "# ==========\nx = 1\n", cc.PYTHON)
        self.assertRule("banner", "// ...---...\nlet a;\n", cc.C_LIKE)
        self.assertNoRule("banner", "// wait for ack ... then retry\nf();\n", cc.C_LIKE)
        self.assertNoRule("banner", "// ... then retry\nf();\n", cc.C_LIKE)

    def test_filler(self):
        self.assertRule("filler", f"// {FILLER} handling the request\nfunction f() {{}}\n", cc.C_LIKE)
        self.assertNoRule("filler", "// Clean up on SIGTERM so the lock file is released\nf();\n", cc.C_LIKE)

    def test_length(self):
        over = f"// {padding(cc.MAX_WORDS + 1)}\nfunction f() {{}}\n"
        at_limit = f"// {padding(cc.MAX_WORDS)}\nfunction f() {{}}\n"
        self.assertRule("long", over, cc.C_LIKE)
        self.assertNoRule("long", at_limit, cc.C_LIKE)

    def test_block(self):
        over = "".join(f"# line{i}\n" for i in range(cc.MAX_LINES)) + "x = 1\n"
        under = "".join(f"# line{i}\n" for i in range(cc.MAX_LINES - 1)) + "x = 1\n"
        self.assertRule("block", over, cc.PYTHON)
        self.assertNoRule("block", under, cc.PYTHON)

    def test_why_comment_kept(self):
        self.assertClean("// Sleep before retry because the API rate-limits bursts\nsleep(1);\n", cc.C_LIKE)
        self.assertClean("# strptime rejects colons in offsets before 3.7\ns = s.replace(':', '')\n", cc.PYTHON)
        self.assertClean("// Must run after auth middleware, see #123\napp.use(x);\n", cc.C_LIKE)

    def test_concise_doc_kept(self):
        text = (
            "/**\n * Resolve a template path relative to the project root.\n"
            " * @param {string} name template name\n * @returns {string} absolute path\n */\n"
            "export function r(name) {}\n"
        )
        self.assertClean(text, cc.C_LIKE)
        self.assertClean('def f():\n    """Return the user id, or None when anonymous."""\n', cc.PYTHON)

    def test_verbose_doc_flagged(self):
        over = f'"""\n{FILLER} {padding(cc.DOC_MAX_WORDS + 1 - FILLER_WORDS)}\n"""\n'
        at_limit = f'"""\n{padding(cc.DOC_MAX_DESC_WORDS)}\n"""\n'
        self.assertRule("filler", over, cc.PYTHON)
        self.assertRule("long", over, cc.PYTHON)
        self.assertNoRule("long", at_limit, cc.PYTHON)

    def test_doc_description_limit(self):
        desc = padding(cc.DOC_MAX_DESC_WORDS + 1)
        text = f'def f():\n    """\n    {desc}\n\n    Returns:\n        int\n    """\n    return 1\n'
        self.assertLessEqual(cc.DOC_MAX_DESC_WORDS + 3, cc.DOC_MAX_WORDS, "test needs desc limit below doc limit")
        self.assertRule("long", text, cc.PYTHON)

    def test_docstring_quote_lines_not_counted(self):
        desc = padding(cc.DOC_MAX_DESC_WORDS)
        text = f'def f():\n    """\n    {desc}\n    """\n    return 1\n'
        self.assertNoRule("long", text, cc.PYTHON)

    def test_doc_label_flagged(self):
        self.assertRule("label", "// Constants\nconst maxRetries = 3\n", cc.GO)
        self.assertRule("banner", "// ===== Helpers =====\nfunc a() {}\n", cc.GO)

    def test_exemptions(self):
        for text, lang in [
            ("# noqa: E501\nx = 1\n", cc.PYTHON),
            ("// eslint-disable-next-line no-console\nconsole.log(1);\n", cc.C_LIKE),
            ("// TODO: handle the error path here\nf();\n", cc.C_LIKE),
            ("// See https://example.com/spec for details\nf();\n", cc.C_LIKE),
            ("# Copyright 2026 Example\nx = 1\n", cc.PYTHON),
            ("// Import the modules pipe-down: keep\nimport x;\n", cc.C_LIKE),
        ]:
            cs = cc.extract_comments(text, lang)
            self.assertTrue(cc.is_exempt(cs[0]), f"expected exemption for {text!r}")

    def test_bdd_markers_exempt_in_test_files(self):
        for text, lang in [
            ("// Given\nvar cart = new Cart();\n", cc.C_LIKE),
            ("// When, then\nassertThrows(X.class, () -> cart.add(null));\n", cc.C_LIKE),
            ("// Then the cart is empty\nassertTrue(cart.isEmpty());\n", cc.C_LIKE),
            ("# and the user is logged out\nassert not session.active\n", cc.PYTHON),
            ("// Arrange\nconst cart = new Cart();\n", cc.C_LIKE),
        ]:
            cs = cc.extract_comments(text, lang)
            self.assertTrue(cc.is_exempt(cs[0], test_file=True), f"expected exemption for {text!r}")
            self.assertFalse(cc.is_exempt(cs[0]), f"exemption must be limited to test files: {text!r}")

    def test_bdd_marker_needs_leading_keyword(self):
        cs = cc.extract_comments("// Runs when the cart is empty\nassertTrue(cart.isEmpty());\n", cc.C_LIKE)
        self.assertFalse(cc.is_exempt(cs[0], test_file=True), "keyword mid-sentence is not a marker")
        doc = cc.extract_comments("/** Given a cart, adds an item. */\nfunction add() {}\n", cc.C_LIKE)
        self.assertFalse(cc.is_bdd_marker(doc[0]), "doc comments are not markers")

    def test_test_path_detection(self):
        for path in [
            "src/test/java/com/example/CartTest.java",
            "src/test/kotlin/CartSpec.kt",
            "Cart.Tests.cs",
            "pkg/cart_test.go",
            "tests/test_cart.py",
            "src/cart.test.ts",
            "src/__tests__/cart.ts",
            "spec/cart_spec.rb",
            "C:\\repo\\tests\\cart.py",
            "src/CartIT.java",
        ]:
            self.assertTrue(cc.is_test_path(path), f"expected test path: {path!r}")
        for path in [
            "src/main/java/com/example/Cart.java",
            "src/latest.py",
            "contest.ts",
            "src/testing.go",
            "src/LIMIT.go",
            "src/SUBMIT.js",
        ]:
            self.assertFalse(cc.is_test_path(path), f"not a test path: {path!r}")


class DiffTests(unittest.TestCase):
    def test_existing_comments_not_reported(self):
        old = "// Import the required modules\nimport fs from 'fs';\n"
        new = old + "const a = 1;\n"
        added = cc.added_comments(cc.extract_comments(old, cc.C_LIKE), cc.extract_comments(new, cc.C_LIKE))
        self.assertEqual(added, [], f"unchanged comment must not be reported as added, got {added!r}")

    def test_rewritten_comment_is_added(self):
        old = "// Import the required modules\nimport fs from 'fs';\n"
        new = "// Import the modules\nimport fs from 'fs';\n"
        self.assertEqual(
            [
                c.text
                for c in cc.added_comments(cc.extract_comments(old, cc.C_LIKE), cc.extract_comments(new, cc.C_LIKE))
            ],
            ["Import the modules"],
            "rewritten comment should count as added",
        )


class JudgeTests(unittest.TestCase):
    def comments(self):
        return cc.extract_comments("x = 1  # set x\n# keep me\ny = 2\n", cc.PYTHON)

    def judge_with(self, stdout):
        class Proc:
            returncode = 0
            stdout = ""

        Proc.stdout = stdout
        original = cc.subprocess.run
        cc.subprocess.run = lambda *a, **k: Proc()
        try:
            return cc.llm_judge(self.comments())
        finally:
            cc.subprocess.run = original

    def test_cli_path_reads_structured_output(self):
        stdout = json.dumps({"structured_output": {"verdicts": [{"id": 0, "verdict": "delete"}]}})
        findings = self.judge_with(stdout)
        self.assertEqual([c.text for c, _ in findings], ["set x"], f"structured verdict not applied: {findings!r}")

    def test_cli_path_ignores_prose_result(self):
        stdout = json.dumps({"result": '{"verdicts":[{"id":0,"verdict":"delete"}]}'})
        findings = self.judge_with(stdout)
        self.assertEqual(findings, [], f"text result without structured_output must fail open, got {findings!r}")

    def test_rewrite_verdict_gives_no_replacement_text(self):
        stdout = json.dumps({"structured_output": {"verdicts": [{"id": 1, "verdict": "rewrite", "text": "Keep this"}]}})
        findings = self.judge_with(stdout)
        self.assertEqual(
            [(c.text, p) for c, p in findings],
            [("keep me", [("judge", "not concise; shorten or remove")])],
            "rewrite verdict should flag the comment without echoing replacement text",
        )

    def test_out_of_range_and_repeated_ids_ignored(self):
        verdicts = [
            {"id": -1, "verdict": "delete"},
            {"id": 0, "verdict": "delete"},
            {"id": 0, "verdict": "rewrite"},
            {"id": 7, "verdict": "delete"},
            {"id": True, "verdict": "delete"},
            "junk",
        ]
        findings = self.judge_with(json.dumps({"structured_output": {"verdicts": verdicts}}))
        self.assertEqual(
            [(c.text, p) for c, p in findings],
            [("set x", [("judge", "not critical to understanding")])],
            f"only the first in-range verdict per id may count, got {findings!r}",
        )

    def test_non_list_verdicts_fail_open(self):
        for payload in [{"verdicts": None}, {"verdicts": 3}, {"verdicts": "x"}, [], 5]:
            findings = self.judge_with(json.dumps({"structured_output": payload}))
            self.assertEqual(findings, [], f"{payload!r} must fail open, got {findings!r}")

    def test_cli_error_payload_allows(self):
        stdout = json.dumps({"is_error": True, "result": "Not logged in"})
        findings = self.judge_with(stdout)
        self.assertEqual(findings, [], f"judge error payload must fail open, got {findings!r}")


class HookTests(unittest.TestCase):
    NARRATIVE = "// Import the required modules\nimport fs from 'fs';\n"

    def test_allows_clean_write(self):
        out = run_hook("Write", {"file_path": "/nonexistent/a.py", "content": "def f():\n    return 1\n"})
        self.assertIsNone(out, f"clean write should be allowed, got {out!r}")

    def test_denies_narrative_write(self):
        reason = deny_reason("Write", {"file_path": "/nonexistent/b.ts", "content": self.NARRATIVE}, session="deny1")
        self.assertIn("line 1", reason, f"reason should name the line: {reason!r}")
        self.assertIn("Keep every other comment in the file as it was.", reason, f"reason: {reason!r}")

    def test_edit_reports_file_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("import os\n\n\ndef f():\n    return 1\n")
            path = fh.name
        reason = deny_reason(
            "Edit",
            {"file_path": path, "old_string": "    return 1\n", "new_string": "    # Return the value\n    return 1\n"},
            session="edit1",
        )
        self.assertIn("line 5", reason, f"Edit should report the line in the file, not the snippet: {reason!r}")

    def test_multiedit(self):
        reason = deny_reason(
            "MultiEdit",
            {
                "file_path": "/nonexistent/c.go",
                "edits": [
                    {"old_string": "a", "new_string": "// Constants\nconst x = 1\n"},
                    {"old_string": "b", "new_string": "y := 2\n"},
                ],
            },
            session="multi1",
        )
        self.assertIn("Constants", reason, f"reason should quote the flagged comment: {reason!r}")

    def test_multiedit_reports_lines_after_earlier_edits(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("import os\n\n\ndef f():\n    return 1\n")
            path = fh.name
        reason = deny_reason(
            "MultiEdit",
            {
                "file_path": path,
                "edits": [
                    {"old_string": "import os\n", "new_string": "import os\nimport sys\n"},
                    {"old_string": "    return 1\n", "new_string": "    # Return the value\n    return 1\n"},
                ],
            },
            session="multi2",
        )
        self.assertIn("line 6", reason, f"line should account for the earlier edit's inserted line: {reason!r}")

    BDD_TEST = (
        "class CartTest {\n"
        "    @Test\n"
        "    void addsItem() {\n"
        "        // Given\n"
        "        var cart = new Cart();\n"
        "        // When\n"
        "        cart.add(item);\n"
        "        // Then\n"
        "        assertEquals(1, cart.size());\n"
        "    }\n"
        "}\n"
    )

    def test_bdd_markers_allowed_in_test_file(self):
        out = run_hook("Write", {"file_path": "/nonexistent/src/test/java/CartTest.java", "content": self.BDD_TEST})
        self.assertIsNone(out, f"BDD markers in a test file must be allowed, got {out!r}")

    def test_bdd_markers_still_checked_outside_tests(self):
        reason = deny_reason("Write", {"file_path": "/nonexistent/src/main/java/Cart.java", "content": self.BDD_TEST})
        self.assertIn("line 8", reason, f"Then should be flagged outside test files: {reason!r}")

    def test_bdd_flag_off(self):
        inp = {"file_path": "/nonexistent/src/test/java/CartTest.java", "content": self.BDD_TEST}
        out = run_hook("Write", inp, session="bdd-off", env_extra={"PIPE_DOWN_BDD": "0"})
        self.assertIsNotNone(out, "PIPE_DOWN_BDD=0 must restore the default checks")

    def test_exempt_comments_not_counted_for_density(self):
        content = "// TODO a\nf();\n// TODO b\ng();\n// TODO c\nh();\n"
        out = run_hook("Write", {"file_path": "/nonexistent/g.ts", "content": content})
        self.assertIsNone(out, f"exempt comments must not trip density, got {out!r}")

    def test_unknown_extension_ignored(self):
        out = run_hook("Write", {"file_path": "/nonexistent/notes.md", "content": "# Imports\n"})
        self.assertIsNone(out, f"unknown extension must be allowed, got {out!r}")

    def test_denial_limit(self):
        inp = {"file_path": "/nonexistent/d.ts", "content": self.NARRATIVE}
        for i in range(cc.MAX_DENIALS):
            reason = run_hook("Write", inp, session="limit")
            self.assertIsNotNone(reason, f"denial {i + 1} of {cc.MAX_DENIALS} should still deny")
        out = run_hook("Write", inp, session="limit")
        self.assertIsNone(out, f"attempt {cc.MAX_DENIALS + 1} should be allowed, got {out!r}")
        self.assertIsNotNone(run_hook("Write", inp, session="limit"), "counter should reset after the allowed edit")

    def test_denial_limit_zero_allows(self):
        inp = {"file_path": "/nonexistent/f.ts", "content": self.NARRATIVE}
        out = run_hook("Write", inp, session="zero", env_extra={"PIPE_DOWN_MAX_DENIALS": "0"})
        self.assertIsNone(out, f"PIPE_DOWN_MAX_DENIALS=0 must allow, got {out!r}")

    def test_judge_default_on(self):
        self.assertTrue(cc.USE_LLM, "judge should default on")

    def test_disable_flag(self):
        inp = {"file_path": "/nonexistent/e.ts", "content": self.NARRATIVE}
        out = run_hook("Write", inp, session="off", env_extra={"PIPE_DOWN_DISABLE": "1"})
        self.assertIsNone(out, f"PIPE_DOWN_DISABLE=1 must allow, got {out!r}")

    def test_judge_command_override(self):
        fake = os.path.join(STATE_DIR, "fake-claude")
        argv_log = os.path.join(STATE_DIR, "fake-argv")
        verdict = {"verdicts": [{"id": 0, "verdict": "delete"}]}
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_log}\n")
            f.write(f"printf '%s' '{json.dumps({'structured_output': verdict})}'\n")
        os.chmod(fake, 0o755)
        inp = {"file_path": "/nonexistent/g.ts", "content": "// retry cap agreed with upstream team\nconst max = 3;\n"}
        env = {"PIPE_DOWN_LLM": "1", "PIPE_DOWN_CLAUDE": f"{fake} --"}
        reason = deny_reason("Write", inp, session="override", env_extra=env)
        self.assertIn("not critical", reason, f"judge delete verdict should appear in reason: {reason!r}")
        with open(argv_log) as f:
            argv = f.read().splitlines()
        self.assertEqual(argv[:2], ["--", "-p"], f"PIPE_DOWN_CLAUDE args should precede -p, got {argv!r}")
        self.assertIn("--json-schema", argv, f"judge should constrain output with --json-schema, got {argv!r}")

    def test_bad_input_allowed(self):
        env = dict(os.environ)
        env["CLAUDE_PLUGIN_DATA"] = STATE_DIR
        env["PIPE_DOWN_LLM"] = "0"
        proc = subprocess.run([sys.executable, SCRIPT], input="not json", capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, f"bad input must exit 0, stderr: {proc.stderr!r}")
        self.assertEqual(proc.stdout, "", f"bad input must produce no decision, got {proc.stdout!r}")


if __name__ == "__main__":
    unittest.main()
