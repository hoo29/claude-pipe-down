#!/usr/bin/env python3
"""PreToolUse hook that rejects low-value comments added by Edit, Write and MultiEdit.

Reads the hook event JSON on stdin. Emits a deny decision on stdout when the edit adds
comments that describe history, restate the code, or are longer than needed. Doc comments
(JSDoc, docstrings, rustdoc and similar) are allowed but held to a length limit.

Configuration is read from environment variables, see README.md.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Configuration


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


MAX_WORDS = _env_int("PIPE_DOWN_MAX_WORDS", 25)
MAX_LINES = _env_int("PIPE_DOWN_MAX_LINES", 3)
DOC_MAX_WORDS = _env_int("PIPE_DOWN_DOC_MAX_WORDS", 60)
DOC_MAX_DESC_WORDS = _env_int("PIPE_DOWN_DOC_MAX_DESC_WORDS", 30)
DENSITY_MIN_COMMENTS = _env_int("PIPE_DOWN_DENSITY_MIN_COMMENTS", 3)
DENSITY_RATIO = _env_int("PIPE_DOWN_DENSITY_PERCENT", 30) / 100.0
MAX_DENIALS = _env_int("PIPE_DOWN_MAX_DENIALS", 2)
USE_LLM = _env_flag("PIPE_DOWN_LLM", True)
LLM_MODEL = os.environ.get("PIPE_DOWN_MODEL", "haiku")
LLM_COMMAND = shlex.split(os.environ.get("PIPE_DOWN_CLAUDE", "")) or ["claude"]
LLM_TIMEOUT = _env_int("PIPE_DOWN_LLM_TIMEOUT", 40)
DISABLED = _env_flag("PIPE_DOWN_DISABLE", False)
ALLOW_BDD = _env_flag("PIPE_DOWN_BDD", True)
KEEP_MARKER = "pipe-down: keep"

# Language table


@dataclass(frozen=True)
class Lang:
    line: Tuple[str, ...] = ()
    block: Optional[Tuple[str, str]] = None
    doc_line: Tuple[str, ...] = ()
    doc_block: Optional[str] = None
    docstring: bool = False
    line_doc: bool = False  # line comments directly above a declaration are doc comments


C_LIKE = Lang(line=("//",), block=("/*", "*/"), doc_line=("///", "//!"), doc_block="/**")
GO = Lang(line=("//",), block=("/*", "*/"), line_doc=True)
HASH = Lang(line=("#",))
SHELL = Lang(line=("#",), line_doc=True)
RUBY = Lang(line=("#",), line_doc=True)
HASH_R = Lang(line=("#",), doc_line=("#'",))
PYTHON = Lang(line=("#",), docstring=True)
POWERSHELL = Lang(line=("#",), block=("<#", "#>"))
HCL = Lang(line=("#", "//"), block=("/*", "*/"))
DASH = Lang(line=("--",), block=("/*", "*/"))
HASKELL = Lang(line=("--",), block=("{-", "-}"), doc_line=("-- |", "-- ^"))
LUA = Lang(line=("--",), block=("--[[", "]]"), doc_line=("---",))
CSS = Lang(block=("/*", "*/"), doc_block="/**")
SCSS = Lang(line=("//",), block=("/*", "*/"), doc_block="/**")
INI = Lang(line=("#", ";"))
LISP = Lang(line=(";",))

EXTENSIONS = {
    **dict.fromkeys(
        (
            "js",
            "jsx",
            "mjs",
            "cjs",
            "ts",
            "tsx",
            "mts",
            "cts",
            "java",
            "kt",
            "kts",
            "rs",
            "c",
            "h",
            "cpp",
            "cc",
            "cxx",
            "hpp",
            "hh",
            "hxx",
            "cs",
            "swift",
            "scala",
            "dart",
            "php",
            "groovy",
            "gradle",
            "proto",
            "sol",
            "zig",
            "vue",
            "svelte",
            "astro",
            "m",
            "mm",
            "d",
            "v",
            "jsonc",
            "json5",
        ),
        C_LIKE,
    ),
    "go": GO,
    **dict.fromkeys(("py", "pyi", "pyx"), PYTHON),
    **dict.fromkeys(("rb", "gemspec", "rake"), RUBY),
    **dict.fromkeys(("sh", "bash", "zsh", "fish", "ksh"), SHELL),
    **dict.fromkeys(
        (
            "yaml",
            "yml",
            "toml",
            "pl",
            "pm",
            "nix",
            "mk",
            "cmake",
            "dockerfile",
            "makefile",
            "gitignore",
            "dockerignore",
            "env",
            "envrc",
            "ex",
            "exs",
            "cr",
            "nim",
            "jl",
            "pp",
            "tcl",
            "awk",
            "sed",
            "bzl",
            "bazel",
        ),
        HASH,
    ),
    "r": HASH_R,
    **dict.fromkeys(("ps1", "psm1", "psd1"), POWERSHELL),
    **dict.fromkeys(("tf", "tfvars", "hcl", "nomad"), HCL),
    **dict.fromkeys(("sql", "psql", "plsql", "ada", "adb", "ads"), DASH),
    **dict.fromkeys(("hs", "lhs", "elm", "purs"), HASKELL),
    "lua": LUA,
    "css": CSS,
    **dict.fromkeys(("scss", "sass", "less"), SCSS),
    **dict.fromkeys(("ini", "cfg", "conf", "properties", "editorconfig"), INI),
    **dict.fromkeys(("el", "clj", "cljs", "cljc", "edn", "lisp", "scm", "rkt"), LISP),
}

BASENAMES = {
    "dockerfile": HASH,
    "makefile": HASH,
    "gnumakefile": HASH,
    "cmakelists.txt": HASH,
    "jenkinsfile": C_LIKE,
    "vagrantfile": RUBY,
    "gemfile": RUBY,
    "rakefile": RUBY,
    "justfile": HASH,
}


def lang_for(path):
    base = os.path.basename(path).lower()
    if base in BASENAMES:
        return BASENAMES[base]
    if base.startswith("dockerfile.") or base.endswith(".dockerfile"):
        return HASH
    _, ext = os.path.splitext(base)
    return EXTENSIONS.get(ext.lstrip(".") or base.lstrip("."))


TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|specs?|__tests__|testing)/|"
    r"(?:^|/)test_[^/]*$|"
    r"(?:_tests?|_specs?|\.tests?|\.specs?|Tests?|Specs?|IT)\.[A-Za-z0-9]+$"
)


def is_test_path(path):
    return bool(TEST_PATH_RE.search(path.replace("\\", "/")))


# Comment extraction


@dataclass
class Comment:
    kind: str  # line, block, doc
    text: str
    start: int  # 0-based line index within the snippet
    end: int
    trailing: bool = False
    same_line_code: str = ""
    next_code: str = ""
    raw: List[str] = field(default_factory=list)

    @property
    def line_count(self):
        return self.end - self.start + 1

    @property
    def key(self):
        return " ".join(self.text.lower().split())


DECL_RE = re.compile(
    r"^\s*(?:export\s+|pub(?:\(crate\))?\s+|public\s+|private\s+|protected\s+|internal\s+|"
    r"static\s+|abstract\s+|final\s+|async\s+|default\s+|declare\s+|unsafe\s+|extern\s+|"
    r"override\s+|open\s+|data\s+|sealed\s+|inline\s+)*"
    r"(?:function|class|interface|type|enum|struct|impl|trait|fn|func|def|mod|module|"
    r"namespace|package|object|record|protocol|extension|resource|variable|output|locals|"
    r"macro_rules!|@\w+|[A-Za-z_][\w-]*\s*\(\s*\)\s*\{?\s*$|"
    r"(?!return\b|await\b|new\b|yield\b|throw\b|else\b|case\b|delete\b|typeof\b|if\b|for\b|"
    r"while\b|switch\b|import\b|export\b|const\b|let\b|var\b|val\b)"
    r"[A-Za-z_][\w<>\[\],\.\*&]*(?:\s+[\w<>\[\],\.\*&]+)*\s+[A-Za-z_]\w*\s*\()"
)

TOP_LEVEL_DECL_RE = re.compile(r"^(?:export\s+|pub\s+)?(?:const|let|var|val|type)\s+[A-Za-z_]")

PY_DOC_OPEN = re.compile(r"""^\s*[rRbBuU]{0,2}("{3}|'{3})""")


def _find_marker(line, markers):
    """Return (index, marker) of the first comment marker outside a string literal."""
    quote = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        for m in markers:
            if line.startswith(m, i):
                if m == "#" and i > 0 and line[i - 1] in "$&{":
                    break
                if m == "//" and i > 0 and line[i - 1] == ":":
                    break
                if m == "--" and i + 2 < n and line[i + 2] in "-=>":
                    # Allow `-->` and `--=`.
                    break
                return i, m
        i += 1
    return None


def _strip_block_line(s, lang):
    s = s.strip()
    if lang.block:
        open_m, close_m = lang.block
        if s.startswith(lang.doc_block or "\0"):
            s = s[len(lang.doc_block) :]
        elif s.startswith(open_m):
            s = s[len(open_m) :]
        if s.endswith(close_m):
            s = s[: -len(close_m)]
        s = s.strip()
        if s.startswith("*") and open_m == "/*":
            s = s[1:]
    return s.strip()


def _clean_text(lines):
    return " ".join(part for part in (ln.strip() for ln in lines) if part)


def extract_comments(text, lang):
    """Return the comments in text, in order, with the code that follows each one."""
    lines = text.split("\n")
    comments = []
    i = 0
    n = len(lines)
    all_markers = tuple(sorted(set(lang.doc_line) | set(lang.line), key=len, reverse=True))

    def is_code(line):
        s = line.strip()
        if not s:
            return False
        if lang.block and s.startswith(lang.block[0]):
            return False
        if lang.docstring and PY_DOC_OPEN.match(s):
            return False
        return all(not s.startswith(m) for m in all_markers)

    def next_code_after(idx):
        for j in range(idx, n):
            if is_code(lines[j]):
                return lines[j].strip()
        return ""

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        # Block comments and docstrings.
        block: Optional[Tuple[str, str]] = None
        doc = False
        doc_match = PY_DOC_OPEN.match(stripped) if lang.docstring else None
        if lang.block and stripped.startswith(lang.block[0]):
            block = lang.block
            doc = lang.doc_block is not None and stripped.startswith(lang.doc_block)
        elif doc_match:
            quote = doc_match.group(1)
            block = (quote, quote)
            doc = True
        if block:
            open_m, close_m = block
            start = i
            body = []
            if lang.doc_block and stripped.startswith(lang.doc_block):
                first = stripped[len(lang.doc_block) :]
            else:
                first = stripped[len(open_m) :]
            if lang.docstring and block[0] in ('"""', "'''"):
                first = re.sub(r"""^[rRbBuU]{0,2}("{3}|'{3})""", "", stripped)
            closed = close_m in first
            if closed:
                first = first[: first.index(close_m)]
            body.append(first)
            i += 1
            while not closed and i < n:
                seg = lines[i]
                if close_m in seg:
                    seg = seg[: seg.index(close_m)]
                    closed = True
                body.append(_strip_block_line(seg, lang) if lang.block and block == lang.block else seg)
                i += 1
            end = i - 1
            comments.append(
                Comment(
                    kind="doc" if doc else "block",
                    text=_clean_text(body),
                    start=start,
                    end=end,
                    next_code=next_code_after(i),
                    raw=lines[start : end + 1],
                )
            )
            continue

        found = _find_marker(line, all_markers)
        if not found:
            i += 1
            continue
        idx, marker = found
        code_before = line[:idx].strip()
        if code_before:
            comments.append(
                Comment(
                    kind="line",
                    text=line[idx + len(marker) :].strip(),
                    start=i,
                    end=i,
                    trailing=True,
                    same_line_code=code_before,
                    raw=[line],
                )
            )
            i += 1
            continue

        # Run of consecutive full-line comments.
        is_doc = marker in lang.doc_line
        start = i
        body = []
        while i < n:
            cur = lines[i]
            f = _find_marker(cur, all_markers)
            if not f or cur[: f[0]].strip():
                break
            m = f[1]
            if (m in lang.doc_line) != is_doc:
                break
            body.append(cur[f[0] + len(m) :])
            i += 1
        end = i - 1
        nxt = next_code_after(i)
        kind = "doc" if is_doc else "line"
        if (
            kind == "line"
            and lang.line_doc
            and nxt
            and i < n
            and lines[i].strip() == nxt
            and (DECL_RE.match(nxt) or (TOP_LEVEL_DECL_RE.match(lines[i]) and not lines[start][0].isspace()))
        ):
            kind = "doc"
        if kind == "line" and start == 0 and lines[0].startswith("#!"):
            body = body[1:]
            start = 1
            if not body:
                continue
        comments.append(
            Comment(kind=kind, text=_clean_text(body), start=start, end=end, next_code=nxt, raw=lines[start : end + 1])
        )
    return comments


# Rules

EXEMPT_RE = re.compile(
    r"(?:^|\W)(?:noqa|type:\s*ignore|pyright:|pylint:|flake8:|mypy:|ruff:|eslint(?:-|\b)|prettier-ignore|"
    r"biome-ignore|@ts-\w+|istanbul|c8\s+ignore|coverage:|nolint|nosec|gosec|#?region\b|#?endregion\b|"
    r"pragma\b|cspell|spell-checker|frozen_string_literal|-\*-\s*coding|rubocop:|nosonar|checkstyle|"
    r"noinspection|swiftlint:|shellcheck|todo\b|fixme\b|xxx\b|hack\b|fmt:\s*(?:on|off)|rustfmt::|"
    r"clippy::|#!\[|#\[|@formatter|deno-lint|webpack\w*:|vite-ignore|jshint|jslint|@flow\b|@jsx\b|"
    r"@vitest|@jest|@__PURE__|sourceMappingURL|vim:|editorconfig|@generated|auto-generated|"
    r"do not edit|licen[cs]e|copyright|spdx-|go:(?:generate|build|embed|linkname)|//export\b|"
    r"language=|@license|@preserve|__attribute__|unused-|ignore-|disable(?:-next-line|-line|:)|"
    r"NOSONAR|sonar\b|ktlint|detekt|@Suppress|Suppress|codeql|semgrep|snyk|trivy|checkov|tfsec|"
    r"terragrunt|ansible-lint|yamllint|markdownlint|dockerfile:|hadolint|editor-fold|renovate|"
    r"dependabot|codecov|bandit|safety|B\d{3}\b|S\d{3,4}\b|E\d{3}\b|W\d{3}\b)",
    re.I,
)
URL_RE = re.compile(r"https?://|www\.", re.I)
BDD_RE = re.compile(r"^(?:given|when|then|and|but|arrange|act|assert)\b", re.I)

HISTORY_RE = re.compile(
    r"\b(?:previously|formerly|originally|used to\b|no longer|"
    r"now (?:uses?|returns?|takes?|handles?|accepts?|supports?|calls?|checks?|does|is|are|has|have|"
    r"includes?|requires?|reads?|writes?|works?|runs?|expects?|allows?|delegates?|wraps?|"
    r"defaults?|passes?|skips?|throws?|raises?|logs?|ignores?|validates?|properly|correctly)|"
    r"changed (?:from|to)|updated (?:to|from|for)|replaced (?:the|with|by)|replaces the (?:old|previous)|"
    r"instead of the (?:old|previous|original)|moved (?:from|to|here|out)|renamed (?:from|to)|"
    r"refactored|rewritten|reworked|removed the|deleted the|dropped the|added (?:support|the|a|an|this)|"
    r"new (?:implementation|version|approach|logic)|old (?:implementation|version|approach|code|logic|"
    r"behaviou?r)|was (?:previously|originally|using|doing)|switched (?:to|from)|reverted|"
    r"fixed (?:the|a|an) (?:bug|issue|problem)|this (?:fix|change|edit|update|patch|refactor)\b|"
    r"as (?:requested|discussed)|per (?:the|your) (?:request|instructions|feedback)|"
    r"before (?:this|the) (?:change|refactor|fix|update)|after (?:the|this) (?:change|refactor|fix|update)|"
    r"(?:the )?(?:previous|earlier|prior) (?:version|implementation|code|behaviou?r|approach)|"
    r"(?:un)?like before|as before|same as before|kept for (?:backward|compat)|"
    r"(?:re)?introduced|brought back|restored (?:the|from)|(?:re)?implemented|simplified from|"
    r"extracted from|inlined from|no longer needed|not needed anymore|leftover|left over)\b",
    re.I,
)

FILLER_RE = re.compile(
    r"\b(?:basically|simply|essentially|in order to|note that|please note|it is important to|"
    r"it's important to|important to note|make sure (?:to|that)|be sure to|we need to|"
    r"we (?:want|use|can|should|must|will|are|have|do|then|now|also|first|need|check|create|call|"
    r"return|get|set|loop|iterate|handle|start|initiali[sz]e)\b|you (?:can|should|need|may|will|might)|"
    r"let'?s|as you can see|obviously|clearly|of course|in this (?:case|function|method|file|section|"
    r"block|class|module)|this (?:function|method|class|file|helper|block|section|code|module|"
    r"variable|constant|loop|component|hook|struct|type|interface|routine|script|snippet) "
    r"(?:is|will|does|handles|takes|returns|checks|creates|gets|sets|provides|contains|represents|"
    r"defines|allows|ensures|performs|calculates|computes|processes|validates|parses|builds|"
    r"initiali[sz]es|implements|manages|loops|iterates|uses|wraps|acts|serves|helps|makes|runs|"
    r"executes|calls|reads|writes|holds|stores|keeps|tracks|maintains|encapsulates)|"
    r"is (?:used|responsible|designed|intended|meant) (?:to|for)|helper (?:function|method) "
    r"(?:to|that|for|which)|a (?:function|method|class|helper|utility) (?:that|to|for|which)|"
    r"responsible for|(?:in charge|takes care) of|the purpose of|the following|here we|"
    r"here,? we|now,? we|first,? we|then,? we|next,? we|finally,? we|lastly,? we|"
    r"as (?:mentioned|noted|stated|described|explained) (?:above|below|earlier|before)|"
    r"in a nutshell|at the end of the day|needless to say|it should be noted|it is worth noting|"
    r"worth noting|keep in mind|bear in mind|remember (?:that|to)|don'?t forget|"
    r"a (?:simple|basic|small|little|quick) (?:function|helper|wrapper|utility|check)|"
    r"nice|neat|clean|elegant|handy|convenient|straightforward|trivial|easy)\b",
    re.I,
)

WHY_RE = re.compile(
    r"\b(?:because|since|otherwise|so that|workaround|work around|avoids?|prevents?|race|deadlock|"
    r"bug|issue|#\d+|cve-|required by|must|cannot|can'?t|won'?t|intentional(?:ly)?|deliberate(?:ly)?|"
    r"on purpose|edge case|corner case|undefined behaviou?r|off-by-one|timezone|dst\b|"
    r"order matters|thread[- ]safe|thread safety|upstream|spec\b|rfc\s*\d|compat|backward|"
    r"quirk|caveat|gotcha|assum\w*|invariant|precondition|postcondition|do not|don'?t|never|"
    r"guarantee|overflow|precision|rounding|hot path|o\(|complexity|security|untrusted|"
    r"sanitiz\w*|escap\w*|inject\w*|unsafe|leak|zero-copy|alloc\w*|blocking|non-blocking|"
    r"idempoten\w*|atomic|lock|mutex|retry|backoff|jitter|throttl\w*|rate.?limit|quota|"
    r"deprecated|legacy|vendor|platform|windows|macos|linux|posix|browser|safari|firefox|"
    r"chrome|ie\d+|polyfill|shim|bypass|trust|permission|privilege|sandbox|"
    r"see\s+\S|cf\.|refs?\b|ref:|\bwhy\b)",
    re.I,
)

NARRATIVE_RE = re.compile(
    r"^(?:now|first(?:ly)?|second(?:ly)?|third|then|next|finally|lastly|here|step\s*\d|"
    r"this (?:function|method|class|file|block|section|code|line|loop|helper|is|will|does|part)|"
    r"we\b|let'?s|start(?:s|ing)? (?:by|the|with)|begin|initiali[sz]e|loop(?:s|ing)? (?:through|over)|"
    r"iterat(?:e|es|ing)|for each|check(?:s|ing)? (?:if|whether|that|for|the)|"
    r"return(?:s|ing)? (?:the|a|an|true|false|null|none|nil|early|if|result|value)|"
    r"creat(?:e|es|ing) (?:a|an|the|new)|get(?:s|ting)? (?:the|a|an|all)|set(?:s|ting)? (?:the|a|an|up)|"
    r"call(?:s|ing)? (?:the|a|an)|import(?:s|ing)?\b|defin(?:e|es|ing)|declar(?:e|es|ing)|"
    r"handl(?:e|es|ing) (?:the|a|an|errors?|exceptions?|case|response|request)|"
    r"process(?:es|ing)? (?:the|a|an|each)|helper|main (?:function|entry|loop)|setup|set up|"
    r"clean ?up|validat(?:e|es|ing) (?:the|a|an|input|that)|pars(?:e|es|ing) (?:the|a|an)|"
    r"build(?:s|ing)? (?:the|a|an)|construct|comput(?:e|es|ing) (?:the|a|an)|"
    r"calculat(?:e|es|ing) (?:the|a|an)|convert(?:s|ing)? (?:the|a|an|to)|updat(?:e|es|ing) (?:the|a|an)|"
    r"add(?:s|ing)? (?:the|a|an|to)|remov(?:e|es|ing) (?:the|a|an|from)|make sure|ensur(?:e|es|ing)|"
    r"open(?:s|ing)? (?:the|a|an)|clos(?:e|es|ing) (?:the|a|an)|read(?:s|ing)? (?:the|a|an|from)|"
    r"writ(?:e|es|ing) (?:the|a|an|to)|send(?:s|ing)? (?:the|a|an)|fetch(?:es|ing)? (?:the|a|an)|"
    r"load(?:s|ing)? (?:the|a|an)|sav(?:e|es|ing) (?:the|a|an)|stor(?:e|es|ing) (?:the|a|an)|"
    r"log(?:s|ging)? (?:the|a|an)|print(?:s|ing)? (?:the|a|an)|appl(?:y|ies|ying) (?:the|a|an)|"
    r"extract(?:s|ing)? (?:the|a|an)|filter(?:s|ing)? (?:the|a|an|out)|sort(?:s|ing)? (?:the|a|an|by)|"
    r"map(?:s|ping)? (?:the|a|an|each|over)|wait(?:s|ing)? for|sleep|delay|try(?:ing)? to|attempt(?:s|ing)? to|"
    r"if the|when the|register|render|mount|unmount|dispatch|emit|subscribe|unsubscribe|"
    r"increment|decrement|append|prepend|push|pop|insert|delete|destroy|dispose|release|"
    r"allocate|free|reset|clear|flush|drain|resolve|reject|throw|raise|catch|wrap|unwrap|"
    r"format|serializ|deserializ|encode|decode|encrypt|decrypt|hash|sign|verify|"
    r"execute|run(?:s|ning)? the|invoke|trigger|notify|broadcast|forward|redirect|route|"
    r"authenticate|authoriz|connect|disconnect|bind|unbind|attach|detach|enable|disable|"
    r"toggle|switch|select|deselect|show|hide|display|draw|paint|animate|scroll|focus|blur|"
    r"exit|quit|abort|cancel|stop|start|restart|pause|resume|continue|skip|ignore the|"
    r"default(?:s)? to|fall(?:s)? back|do the|does the|perform)\b",
    re.I,
)

LABEL_RE = re.compile(
    r"^(?:constants?|variables?|imports?|exports?|types?|interfaces?|helpers?|utilities|utils|"
    r"setup|teardown|config(?:uration)?|state|props|methods?|handlers?|listeners?|event handlers?|"
    r"public(?: (?:api|methods?|members?))?|private(?: (?:methods?|members?|helpers?))?|internal|api|"
    r"routes?|middlewares?|models?|views?|controllers?|services?|tests?|main|entry ?point|"
    r"initiali[sz]ation|init|cleanup|globals?|fields?|properties|getters?(?: and setters?)?|setters?|"
    r"lifecycle|hooks?|components?|render(?:ing)?|styles?|dependencies|declarations?|definitions?|"
    r"implementation|logic|business logic|core|misc(?:ellaneous)?|other|end(?: of)?(?: \w+)?|"
    r"start(?: of)?(?: \w+)?|begin(?:ning)?(?: of)?(?: \w+)?|body|header|footer|"
    r"error handling|errors?|exceptions?|validation|parsing|processing|output|input|"
    r"arguments?|args|options?|flags|parameters?|params|defaults?|overrides?|"
    r"class (?:definition|declaration|body)|function (?:definition|declaration|body)|"
    r"module (?:exports?|definition)|type (?:definitions?|declarations?|aliases)|"
    r"event (?:listeners?|handlers?|handling)|data|results?|response|request|loop|"
    r"main (?:loop|function|logic|entry)|exports? (?:section|block))\s*:?$",
    re.I,
)

BANNER_RE = re.compile(r"(?:^|\s)[-=*#_~+.─-╿<>/\\|]{3,}(?:\s|$)")
RAW_BANNER_RE = re.compile(r"^\s*(?://|#|--|;|/\*|\*)\s*[-=*#_~+.─-╿<>/\\|]{3,}")

STOPWORDS = frozenset(
    "the a an and or of to for in on at by with from is are be been being this that these those it its we if then "
    "as into each all any some no not our your their his her them they he she you i me my what which who whom whose "
    "when where how than too also just only very can will would should could may might must shall do does did done "
    "has have had having was were am s t re ve ll d m up out off over under again further once here there why so "
    "such both more most other own same up down ".split()
)

CODE_KEYWORDS = frozenset(
    "return if else for while do switch case break continue new delete try catch finally throw import export from "
    "as class def function fn func let const var val public private static void async await yield with in is not "
    "and or pass raise lambda self this super null none nil true false struct enum interface type impl trait pub "
    "use mod match where loop go defer select chan map range print println ".split()
)


def _split_ident(token):
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", token).replace("_", " ").split()
    return [p.lower() for p in parts]


def _stem(word):
    for suffix in ("ations", "ation", "ings", "ing", "ies", "ied", "ers", "er", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _comment_tokens(text):
    out = []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        for part in _split_ident(tok):
            if part in STOPWORDS or len(part) < 3:
                continue
            out.append(_stem(part))
    return out


def _code_tokens(code):
    out = set()
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code):
        for part in _split_ident(tok):
            if len(part) < 2:
                continue
            out.add(_stem(part))
            out.add(part)
    return out


def _word_count(text):
    return len(re.findall(r"\S+", text))


DOC_SECTION_RE = re.compile(
    r"^(?::\w+|Args|Arguments|Returns?|Raises|Yields|Params?|Parameters|Example|Examples|Note|Notes|"
    r"See|Throws|Type|Attributes)\b"
)


def _doc_description_words(comment):
    words = 0
    for raw in comment.raw:
        s = raw.strip().lstrip("/*#'\"!- ").strip()
        if s.startswith("@") or DOC_SECTION_RE.match(s):
            break
        words += _word_count(s)
    return words


def restates_code(comment):
    code = comment.same_line_code if comment.trailing else comment.next_code
    if not code:
        return False
    ctoks = _comment_tokens(comment.text)
    if not ctoks:
        return False
    codetoks = _code_tokens(code)
    overlap = sum(1 for t in ctoks if t in codetoks)
    ratio = overlap / len(ctoks)
    if len(ctoks) <= 2:
        return overlap >= 1 and ratio >= 0.5
    return overlap >= 2 and ratio >= 0.5


def is_bdd_marker(comment):
    """Given/When/Then and Arrange/Act/Assert markers that structure a test body."""
    return comment.kind != "doc" and BDD_RE.match(comment.text.strip()) is not None


def is_exempt(comment, test_file=False):
    text = comment.text
    if KEEP_MARKER in text.lower():
        return True
    if not text.strip():
        return True
    if EXEMPT_RE.search(text) or URL_RE.search(text):
        return True
    if test_file and ALLOW_BDD and is_bdd_marker(comment):
        return True
    return bool(comment.raw and comment.raw[0].startswith("#!"))


def find_problems(comment):
    """Return a list of (rule, detail) tuples for a single added comment."""
    problems = []
    text = comment.text
    words = _word_count(text)

    if HISTORY_RE.search(text):
        problems.append(("history", "describes a change rather than the current code"))

    fill = FILLER_RE.search(text)
    if fill:
        problems.append(("filler", f"filler wording: '{fill.group(0)}'"))

    if comment.kind == "doc":
        if BANNER_RE.search(text) or any(RAW_BANNER_RE.match(r) for r in comment.raw):
            problems.append(("banner", "decorative banner"))
            return problems
        if LABEL_RE.match(text.strip()):
            problems.append(("label", "section label"))
        if words > DOC_MAX_WORDS:
            problems.append(("long", f"{words} words, doc comment limit is {DOC_MAX_WORDS}"))
        else:
            desc = _doc_description_words(comment)
            if desc > DOC_MAX_DESC_WORDS:
                problems.append(("long", f"{desc}-word description, limit is {DOC_MAX_DESC_WORDS}"))
        return problems

    if words > MAX_WORDS:
        problems.append(("long", f"{words} words, limit is {MAX_WORDS}"))
    if comment.line_count >= MAX_LINES:
        problems.append(("block", f"{comment.line_count}-line comment block, limit is {MAX_LINES - 1}"))

    if BANNER_RE.search(text) or any(RAW_BANNER_RE.match(r) for r in comment.raw):
        problems.append(("banner", "decorative banner"))
        return problems

    if WHY_RE.search(text):
        return problems

    if LABEL_RE.match(text.strip()):
        problems.append(("label", "section label"))
    elif restates_code(comment):
        problems.append(("restate", "restates the code %s it" % ("beside" if comment.trailing else "below")))
    elif NARRATIVE_RE.match(text.strip()):
        problems.append(("narrative", "narrates what the code does"))
    return problems


# Diffing


def added_comments(old_text, new_text, lang):
    old_counts = Counter(c.key for c in extract_comments(old_text, lang)) if old_text else Counter()
    added = []
    for c in extract_comments(new_text, lang):
        if old_counts[c.key] > 0:
            old_counts[c.key] -= 1
            continue
        added.append(c)
    return added


def code_line_count(text, lang):
    comment_lines = set()
    for c in extract_comments(text, lang):
        if not c.trailing:
            comment_lines.update(range(c.start, c.end + 1))
    return sum(1 for i, ln in enumerate(text.split("\n")) if ln.strip() and i not in comment_lines)


def read_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def line_offset(file_text, old_string):
    if not old_string:
        return 0
    idx = file_text.find(old_string)
    if idx < 0:
        return None
    return file_text.count("\n", 0, idx)


# Optional model judge

LLM_SYSTEM = """You review code comments written by an AI assistant. Apply these rules strictly:
1. Only comment when it is critical to understanding the code. Comments that restate what the code visibly does,
   narrate steps, label sections, or explain standard language features are not critical.
2. Never describe history: what changed, what it used to be, why an edit was made.
3. If a comment is critical, every word must count. Plain English, no filler, as short as possible.
4. Doc comments (JSDoc, docstrings, rustdoc, javadoc) are allowed for public API but must be concise: one short
   sentence for the summary, tags only where they add information the signature does not.
When unsure, delete. Respond with JSON only, no prose:
{"verdicts":[{"id":<number>,"verdict":"keep"|"delete"|"rewrite"}]}"""


def _judge_via_cli(prompt):
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    proc = subprocess.run(
        [
            *LLM_COMMAND,
            "-p",
            "--tools",
            "",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--model",
            LLM_MODEL,
            "--output-format",
            "json",
            "--system-prompt",
            LLM_SYSTEM,
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=LLM_TIMEOUT,
        env=env,
    )
    if proc.returncode != 0:
        return ""
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict) or payload.get("is_error"):
        return ""
    return payload.get("result", "")


def llm_judge(comments):
    if not comments:
        return []
    items = []
    for n, c in enumerate(comments):
        code = c.same_line_code if c.trailing else c.next_code
        items.append({"id": n, "kind": c.kind, "comment": c.text, "code_after": code})
    prompt = "Comments to review:\n" + json.dumps(items, indent=1)
    try:
        result = _judge_via_cli(prompt)
        m = re.search(r"\{.*\}", result, re.S)
        verdicts = json.loads(m.group(0))["verdicts"] if m else []
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return []
    findings = []
    for v in verdicts:
        try:
            c = comments[int(v["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        verdict = str(v.get("verdict", "")).lower()
        if verdict == "delete":
            findings.append((c, [("judge", "not critical to understanding")]))
        elif verdict == "rewrite":
            findings.append((c, [("judge", "not concise; shorten or remove")]))
    return findings


# Denial bookkeeping


def state_path():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".claude", "pipe-down")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        return None
    return os.path.join(base, "denials.json")


def load_state():
    path = state_path()
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - 86400
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("t", 0) > cutoff}


def save_state(state):
    path = state_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


# Main


def collect_changes(tool_name, tool_input):
    """Return (file_path, [(old_text, new_text, old_string)]) for the tool call."""
    path = tool_input.get("file_path", "")
    if tool_name == "Write":
        return path, [(read_file(path), tool_input.get("content", ""), None)]
    if tool_name == "Edit":
        return path, [
            (tool_input.get("old_string", ""), tool_input.get("new_string", ""), tool_input.get("old_string", ""))
        ]
    if tool_name == "MultiEdit":
        return path, [
            (e.get("old_string", ""), e.get("new_string", ""), e.get("old_string", ""))
            for e in tool_input.get("edits", [])
        ]
    return path, []


def format_reason(path, findings, density):
    lines = [
        "pipe-down: this edit adds comments that break the comment rules. "
        "Remove or shorten only the comments listed below, then resubmit."
    ]
    for c, problems, line_no in findings:
        loc = f"line {line_no}" if line_no is not None else f"snippet line {c.start + 1}"
        preview = c.text if len(c.text) <= 80 else c.text[:77] + "..."
        lines.append('- {}: "{}" [{}]'.format(loc, preview, "; ".join(d for _, d in problems)))
    if density:
        lines.append(f"- {density}")
    lines.append(
        "Rules: comment only when critical to understanding the code. Never describe what changed or what "
        "was there before. When a comment is needed, use plain English and as few words as possible. "
        "Doc comments on public API are fine but must be concise. Do not re-add the listed comments in a "
        "different form. Keep every other comment in the file as it was."
    )
    return "\n".join(lines)


def emit(decision, reason=None):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}}
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def main():
    if DISABLED:
        return 0
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    path, changes = collect_changes(tool_name, tool_input)
    if not path or not changes:
        return 0
    lang = lang_for(path)
    if lang is None:
        return 0

    file_text = read_file(path) if tool_name != "Write" else ""
    test_file = is_test_path(path)
    findings = []
    unflagged = []
    total_added = 0
    total_code = 0
    for old_text, new_text, old_string in changes:
        offset = line_offset(file_text, old_string) if old_string is not None else 0
        if old_string and offset is not None:
            file_text = file_text.replace(old_string, new_text, 1)
        added = added_comments(old_text, new_text, lang)
        total_code += max(0, code_line_count(new_text, lang) - (code_line_count(old_text, lang) if old_text else 0))
        for c in added:
            if is_exempt(c, test_file):
                continue
            if c.kind != "doc":
                total_added += 1
            problems = find_problems(c)
            line_no = (offset + c.start + 1) if offset is not None else None
            if problems:
                findings.append((c, problems, line_no))
            elif c.kind != "doc":
                unflagged.append((c, line_no))

    density = None
    if total_added >= DENSITY_MIN_COMMENTS and total_code > 0 and total_added / total_code >= DENSITY_RATIO:
        density = f"{total_added} comments added for {total_code} lines of code; most of these are not critical"

    if USE_LLM and unflagged:
        judged = llm_judge([c for c, _ in unflagged])
        line_by_id = {id(c): ln for c, ln in unflagged}
        for c, problems in judged:
            findings.append((c, problems, line_by_id.get(id(c))))

    session = event.get("session_id", "")
    key = f"{session}|{path}"
    state = load_state()
    if not findings and not density:
        if key in state:
            del state[key]
            save_state(state)
        return 0

    count = state.get(key, {}).get("n", 0)
    if count >= MAX_DENIALS:
        state.pop(key, None)
        save_state(state)
        sys.stderr.write(f"pipe-down: denial limit reached for {path}, allowing edit\n")
        return 0
    state[key] = {"n": count + 1, "t": time.time()}
    save_state(state)
    findings.sort(key=lambda f: (f[2] if f[2] is not None else 10**9, f[0].start))
    emit("deny", format_reason(path, findings, density))
    return 0


if __name__ == "__main__":
    sys.exit(main())
