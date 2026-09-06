# CLAUDE.md

Claude Code plugin. A PreToolUse hook on Edit, Write and MultiEdit that rejects edits adding
low-value comments. Everything lives in `hooks/check_comments.py`; there are no dependencies.

## Layout

- `.claude-plugin/plugin.json`: plugin manifest. `marketplace.json` makes the repo its own marketplace.
- `hooks/hooks.json`: hook registration. Command is `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/check_comments.py"`.
- `hooks/check_comments.py`: extraction, rules, diffing, denial counter, optional model judge.
- `tests/test_check_comments.py`: unittest suite. Run with `python3 -m unittest discover -s tests`.
- `pyproject.toml`: ruff and pyright config. Run `ruff check .`, `ruff format .` and `pyright` before finishing.

## Constraints

- Python 3.8 compatible, stdlib only. CI runs 3.8 and 3.12. Use `typing` generics, not `list[str]`.
- Fail open. Any parse error, missing file, unknown extension or judge failure must allow the edit.
- Only comments added by the edit are judged. Do not break the old versus new diff in `added_comments`.
- Output is JSON on stdout with `permissionDecision: deny`, exit 0. Never exit 2.
- Keep the hook fast. The regex path runs on every file edit. The judge only runs when unflagged
  comments remain, and must stay switchable with `PIPE_DOWN_LLM=0`.
- Rule changes need a test in `RuleTests`. Extraction changes need a test in `ExtractionTests`.
- README tables and the config variable list must match the code.
- CI (`.github/workflows/ci.yml`) runs ruff, pyright, the tests and a manifest JSON check. All must pass.

## Rules the hook enforces

1. Comment only when critical to understanding the code.
2. Never describe what changed or what was there before.
3. When a comment is needed, plain English, as few words as possible.
4. Doc comments are allowed but concise.

Apply the same rules to comments in this repo.
