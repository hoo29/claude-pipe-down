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
    assert out is not None, "expected a deny decision"
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def problems(text, lang):
    comments = cc.extract_comments(text, lang)
    assert comments, f"no comment extracted from {text!r}"
    return [rule for rule, _ in cc.find_problems(comments[0])]


class ExtractionTests(unittest.TestCase):
    def test_line_and_trailing(self):
        text = "// top\nconst a = 1; // trailing\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual([(c.text, c.trailing) for c in cs], [("top", False), ("trailing", True)])
        self.assertEqual(cs[0].next_code, "const a = 1; // trailing")
        self.assertEqual(cs[1].same_line_code, "const a = 1;")

    def test_marker_inside_string_ignored(self):
        text = 'const url = "http://example.com"; // real\n'
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual([c.text for c in cs], ["real"])

    def test_hash_in_shell_expansion_ignored(self):
        text = 'echo "${#arr[@]}" $# # count\n'
        cs = cc.extract_comments(text, cc.HASH)
        self.assertEqual([c.text for c in cs], ["count"])

    def test_consecutive_lines_merge(self):
        text = "# one\n# two\nx = 1\n"
        cs = cc.extract_comments(text, cc.PYTHON)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].line_count, 2)
        self.assertEqual(cs[0].text, "one two")

    def test_jsdoc_is_doc(self):
        text = "/**\n * Summary.\n * @param {string} x name\n */\nfunction f(x) {}\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual(cs[0].kind, "doc")
        self.assertEqual(cs[0].next_code, "function f(x) {}")

    def test_python_docstring_is_doc(self):
        text = 'def f():\n    """Summary."""\n    return 1\n'
        cs = cc.extract_comments(text, cc.PYTHON)
        self.assertEqual(cs[0].kind, "doc")
        self.assertEqual(cs[0].text, "Summary.")

    def test_go_decl_comment_is_doc(self):
        text = "// Parse reads the config.\nfunc Parse() {}\n"
        self.assertEqual(cc.extract_comments(text, cc.GO)[0].kind, "doc")
        self.assertEqual(cc.extract_comments("# Usage: run.sh <dir>\nrun() {\n}\n", cc.SHELL)[0].kind, "doc")

    def test_js_line_comment_before_function_is_not_doc(self):
        text = "// Parse reads the config.\nfunction parse() {}\n"
        self.assertEqual(cc.extract_comments(text, cc.C_LIKE)[0].kind, "line")

    def test_comment_before_local_const_is_not_doc(self):
        text = "function f() {\n  // Read the file\n  const raw = read();\n}\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual(cs[0].kind, "line")

    def test_rust_doc_line(self):
        text = "/// Parses input.\npub fn parse() {}\n"
        cs = cc.extract_comments(text, cc.C_LIKE)
        self.assertEqual(cs[0].kind, "doc")

    def test_lang_lookup(self):
        self.assertIs(cc.lang_for("/x/Dockerfile"), cc.HASH)
        self.assertIs(cc.lang_for("/x/main.go"), cc.GO)
        self.assertIs(cc.lang_for("/x/run.sh"), cc.SHELL)
        self.assertIs(cc.lang_for("/x/a.tsx"), cc.C_LIKE)
        self.assertIsNone(cc.lang_for("/x/README.md"))
        self.assertIs(cc.lang_for("/x/.gitignore"), cc.HASH)
        self.assertIs(cc.lang_for("/x/.env"), cc.HASH)
        self.assertIs(cc.lang_for("/x/.editorconfig"), cc.INI)

    def test_comment_after_shebang_is_kept(self):
        text = "#!/usr/bin/env python\n# Import the modules\nimport os\n"
        cs = cc.extract_comments(text, cc.PYTHON)
        self.assertEqual([c.text for c in cs], ["Import the modules"])
        self.assertEqual(cs[0].raw, ["# Import the modules"])
        self.assertFalse(cc.is_exempt(cs[0]))
        self.assertEqual(cc.extract_comments("#!/bin/sh\necho hi\n", cc.SHELL), [])


class RuleTests(unittest.TestCase):
    def test_history(self):
        self.assertIn("history", problems("// Changed to use async instead of sync\nawait f();\n", cc.C_LIKE))
        self.assertIn("history", problems("# Previously this returned a list\nreturn x\n", cc.PYTHON))
        self.assertIn("history", problems("# No longer needed after the refactor\nreturn x\n", cc.PYTHON))

    def test_restate(self):
        self.assertIn("restate", problems("// Parse and return the JSON\nreturn JSON.parse(raw);\n", cc.C_LIKE))
        self.assertIn("restate", problems("# Set the port\nport: 80\n", cc.HASH))
        self.assertIn("restate", problems("x = load_users()  # load users\n", cc.PYTHON))

    def test_narrative(self):
        self.assertIn("narrative", problems("// Import the required modules\nimport fs from 'fs';\n", cc.C_LIKE))
        self.assertIn("narrative", problems("// Now we iterate over each entry\nfor (const e of xs) {}\n", cc.C_LIKE))

    def test_label_and_banner(self):
        self.assertIn("label", problems("// Helpers\nfunction a() {}\n", cc.C_LIKE))
        self.assertIn("label", problems("# Imports\nimport os\n", cc.PYTHON))
        self.assertIn("banner", problems("// ------ Setup ------\nlet a;\n", cc.C_LIKE))
        self.assertIn("banner", problems("# ==========\nx = 1\n", cc.PYTHON))

    def test_filler_and_length(self):
        p = problems(
            "// This function is responsible for handling the request and it also validates every field "
            "before returning\nfunction f() {}\n",
            cc.C_LIKE,
        )
        self.assertIn("filler", p)
        self.assertIn("long", p)

    def test_block(self):
        p = problems("# a\n# b\n# c\nx = 1\n", cc.PYTHON)
        self.assertIn("block", p)

    def test_why_comment_kept(self):
        self.assertEqual(
            problems("// Sleep before retry because the API rate-limits bursts\nsleep(1);\n", cc.C_LIKE), []
        )
        self.assertEqual(
            problems("# strptime rejects colons in offsets before 3.7\ns = s.replace(':', '')\n", cc.PYTHON), []
        )
        self.assertEqual(problems("// Must run after auth middleware, see #123\napp.use(x);\n", cc.C_LIKE), [])

    def test_concise_doc_kept(self):
        text = (
            "/**\n * Resolve a template path relative to the project root.\n"
            " * @param {string} name template name\n * @returns {string} absolute path\n */\n"
            "export function r(name) {}\n"
        )
        self.assertEqual(problems(text, cc.C_LIKE), [])
        self.assertEqual(problems('def f():\n    """Return the user id, or None when anonymous."""\n', cc.PYTHON), [])

    def test_verbose_doc_flagged(self):
        text = (
            '"""\nThis function is responsible for parsing the input string that is passed to it and it will '
            'then return the parsed value back to the caller so that the caller can use it.\n"""\n'
        )
        p = problems(text, cc.PYTHON)
        self.assertIn("filler", p)
        self.assertIn("long", p)

    def test_docstring_quote_lines_not_counted(self):
        desc = " ".join(["word"] * cc.DOC_MAX_DESC_WORDS)
        text = f'def f():\n    """\n    {desc}\n    """\n    return 1\n'
        self.assertNotIn("long", problems(text, cc.PYTHON))

    def test_doc_label_flagged(self):
        self.assertIn("label", problems("// Constants\nconst maxRetries = 3\n", cc.GO))
        self.assertIn("banner", problems("// ===== Helpers =====\nfunc a() {}\n", cc.GO))

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
            self.assertTrue(cc.is_exempt(cs[0]), text)


class DiffTests(unittest.TestCase):
    def test_existing_comments_not_reported(self):
        old = "// Import the required modules\nimport fs from 'fs';\n"
        new = old + "const a = 1;\n"
        self.assertEqual(cc.added_comments(old, new, cc.C_LIKE), [])

    def test_rewritten_comment_is_added(self):
        old = "// Import the required modules\nimport fs from 'fs';\n"
        new = "// Import the modules\nimport fs from 'fs';\n"
        self.assertEqual([c.text for c in cc.added_comments(old, new, cc.C_LIKE)], ["Import the modules"])


class JudgeTests(unittest.TestCase):
    def comments(self):
        return cc.extract_comments("x = 1  # set x\n# keep me\ny = 2\n", cc.PYTHON)

    def test_cli_path_parses_fenced_json(self):
        class Proc:
            returncode = 0
            stdout = json.dumps({"result": '```json\n{"verdicts":[{"id":0,"verdict":"delete"}]}\n```'})

        original = cc.subprocess.run
        cc.subprocess.run = lambda *a, **k: Proc()
        try:
            findings = cc.llm_judge(self.comments())
        finally:
            cc.subprocess.run = original
        self.assertEqual([c.text for c, _ in findings], ["set x"])

    def test_cli_error_payload_allows(self):
        class Proc:
            returncode = 0
            stdout = json.dumps({"is_error": True, "result": "Not logged in"})

        original = cc.subprocess.run
        cc.subprocess.run = lambda *a, **k: Proc()
        try:
            self.assertEqual(cc.llm_judge(self.comments()), [])
        finally:
            cc.subprocess.run = original


class HookTests(unittest.TestCase):
    def test_allows_clean_write(self):
        out = run_hook("Write", {"file_path": "/nonexistent/a.py", "content": "def f():\n    return 1\n"})
        self.assertIsNone(out)

    def test_denies_narrative_write(self):
        reason = deny_reason(
            "Write",
            {"file_path": "/nonexistent/b.ts", "content": "// Import the required modules\nimport fs from 'fs';\n"},
            session="deny1",
        )
        self.assertIn("line 1", reason)

    def test_edit_reports_file_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("import os\n\n\ndef f():\n    return 1\n")
            path = fh.name
        reason = deny_reason(
            "Edit",
            {"file_path": path, "old_string": "    return 1\n", "new_string": "    # Return the value\n    return 1\n"},
            session="edit1",
        )
        self.assertIn("line 5", reason)

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
        self.assertIn("Constants", reason)

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
        self.assertIn("line 6", reason)

    def test_unknown_extension_ignored(self):
        out = run_hook("Write", {"file_path": "/nonexistent/notes.md", "content": "# Imports\n"})
        self.assertIsNone(out)

    def test_denial_limit(self):
        inp = {"file_path": "/nonexistent/d.ts", "content": "// Import the required modules\nimport fs from 'fs';\n"}
        deny_reason("Write", inp, session="limit")
        deny_reason("Write", inp, session="limit")
        self.assertIsNone(run_hook("Write", inp, session="limit"))
        deny_reason("Write", inp, session="limit")

    def test_denial_limit_zero_allows(self):
        inp = {"file_path": "/nonexistent/f.ts", "content": "// Import the required modules\nimport fs from 'fs';\n"}
        self.assertIsNone(run_hook("Write", inp, session="zero", env_extra={"PIPE_DOWN_MAX_DENIALS": "0"}))

    def test_judge_default_on(self):
        self.assertTrue(cc.USE_LLM)

    def test_disable_flag(self):
        inp = {"file_path": "/nonexistent/e.ts", "content": "// Import the required modules\nimport fs from 'fs';\n"}
        self.assertIsNone(run_hook("Write", inp, session="off", env_extra={"PIPE_DOWN_DISABLE": "1"}))

    def test_judge_command_override(self):
        fake = os.path.join(STATE_DIR, "fake-claude")
        argv_log = os.path.join(STATE_DIR, "fake-argv")
        verdict = '{"verdicts":[{"id":0,"verdict":"delete"}]}'
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_log}\n")
            f.write(f"printf '%s' '{json.dumps({'result': verdict})}'\n")
        os.chmod(fake, 0o755)
        inp = {"file_path": "/nonexistent/g.ts", "content": "// retry cap agreed with upstream team\nconst max = 3;\n"}
        env = {"PIPE_DOWN_LLM": "1", "PIPE_DOWN_CLAUDE": f"{fake} --"}
        reason = deny_reason("Write", inp, session="override", env_extra=env)
        self.assertIn("not critical", reason)
        with open(argv_log) as f:
            argv = f.read().splitlines()
        self.assertEqual(argv[:2], ["--", "-p"])

    def test_bad_input_allowed(self):
        env = dict(os.environ)
        env["CLAUDE_PLUGIN_DATA"] = STATE_DIR
        env["PIPE_DOWN_LLM"] = "0"
        proc = subprocess.run([sys.executable, SCRIPT], input="not json", capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
