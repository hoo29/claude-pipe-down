# claude-pipe-down

A Claude Code plugin that stops Claude over-commenting code. It runs as a `PreToolUse` hook on
`Edit`, `Write` and `MultiEdit`, inspects the comments the edit would add, and rejects the edit
with a line-by-line reason when they break the rules below. Claude resubmits without them.

Rules enforced:

1. Comment only when it is critical to understanding the code.
2. Never describe what changed or what was there before.
3. When a comment is needed, use plain English and as few words as possible.
4. Doc comments (JSDoc, docstrings, rustdoc, Go and Ruby declaration comments) are allowed but
   must be concise.

Only comments added by the edit are checked. Comments already in the file are left alone, so the
hook does not nag about existing code.

## Install

```
claude plugin marketplace add hoo29/claude-pipe-down
claude plugin install pipe-down@claude-pipe-down
```

Requires `python3` on `PATH`. No third-party packages.

## What gets flagged

| Rule | Example | Reason given |
| --- | --- | --- |
| history | `// Changed to use async instead of sync` | describes a change rather than the current code |
| restate | `// Parse and return the JSON` above `return JSON.parse(raw)` | restates the code below it |
| narrative | `// Import the required modules` | narrates what the code does |
| label | `// Helpers`, `# Imports` | section label |
| banner | `// ---------- Setup ----------` | decorative banner |
| filler | `This function is responsible for ...`, `we need to`, `note that` | filler wording |
| long | non-doc comment over 15 words | word limit |
| block | 3 or more consecutive comment lines | comment block |
| density | 3 or more comments at over 30 percent of added code lines | too many comments |

Doc comments are checked for history, filler, labels, banners and length (60 words total,
30 for the description before any tags). They are not checked for restating or narrating,
since a summary line is expected to describe the declaration.

Never flagged: linter and compiler directives, `TODO` and `FIXME`, URLs, license and copyright
headers, shebangs, and any comment containing `pipe-down: keep`.

A comment that explains why is kept even when it also matches the restate or narrative rule.
Words such as because, otherwise, workaround, race, must, never, deprecated, spec, RFC and
issue references mark a comment as explanatory.

## Loop guard

If the same file is denied twice in one session the third attempt is allowed, so an unhelpful
heuristic cannot block progress. The counter resets after any allowed edit to that file.

## Model judge

Regex catches history, verbosity and the common restatement patterns. Whether a comment is
critical is a judgment call, so comments that pass the regex stage are sent to Claude Haiku for a
keep, delete or rewrite verdict. The judge is on by default. Set `PIPE_DOWN_LLM=0` to turn it off.

The judge only runs when an edit adds comments the regex did not already reject, which is a
minority of edits. All comments from one edit go in a single request. Requests cannot be batched
across edits because the hook has to answer before each edit runs.

The request runs through `claude -p` with the auth Claude Code already has, including a
subscription. The CLI adds around 20k tokens of its own context per request. Measured cost was
under one cent when that context was cached and about five cents when it was not.

If the request fails for any reason the edit is allowed. `PIPE_DOWN_MODEL` accepts `haiku`,
`sonnet`, `opus` or a full model id.

## Configuration

Set these in the `env` block of `settings.json` or in the shell that launches Claude Code.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PIPE_DOWN_DISABLE` | `0` | Set to `1` to turn the hook off |
| `PIPE_DOWN_MAX_WORDS` | `15` | Word limit for a non-doc comment |
| `PIPE_DOWN_MAX_LINES` | `3` | Consecutive comment lines that count as a block |
| `PIPE_DOWN_DOC_MAX_WORDS` | `60` | Word limit for a doc comment |
| `PIPE_DOWN_DOC_MAX_DESC_WORDS` | `30` | Word limit for a doc comment description before tags |
| `PIPE_DOWN_DENSITY_MIN_COMMENTS` | `3` | Minimum added comments before density is checked |
| `PIPE_DOWN_DENSITY_PERCENT` | `30` | Comments as a percentage of added code lines |
| `PIPE_DOWN_MAX_DENIALS` | `2` | Denials per file per session before the edit is allowed |
| `PIPE_DOWN_LLM` | `1` | Set to `0` to disable the model judge |
| `PIPE_DOWN_MODEL` | `haiku` | Judge model: `haiku`, `sonnet`, `opus` or a full model id |
| `PIPE_DOWN_LLM_TIMEOUT` | `40` | Seconds to wait for the judge |

## Supported languages

Comment syntax is chosen by file extension. Covered: C family, JavaScript and TypeScript, Java,
Kotlin, Go, Rust, Swift, C#, PHP, Dart, Scala, Python, Ruby, shell, YAML, TOML, Terraform and HCL,
SQL, Lua, Haskell, CSS and preprocessors, PowerShell, INI, Lisp family, Dockerfile and Makefile.
Markdown, HTML and XML are ignored.

## Development

```
python3 -m unittest discover -s tests -v
ruff check .
ruff format .
pyright
```

CI runs ruff, pyright, the tests on Python 3.8 and 3.12, and a JSON syntax check on the manifests.

Test a change by hand:

```
echo '{"tool_name":"Write","tool_input":{"file_path":"/tmp/a.ts","content":"// Import fs\nimport fs from \"fs\";\n"}}' \
  | python3 hooks/check_comments.py
```

Use `CLAUDE_PLUGIN_DATA` to point the denial counter at a scratch directory when testing.

## Limitations

Heuristics judge text, not intent. A short explanatory comment without a why-word can be flagged
as restatement. The loop guard and the `pipe-down: keep` marker exist for those cases.
Comment detection is line based and string aware, but regex literals containing `//` and
unusual multi-line string forms can confuse it.
