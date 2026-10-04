"""PreToolUse hook: auto-approve plain `sed` text edits on files inside this repo.

Approves only when ALL of these hold; anything else gets no decision, so the normal permission
prompt appears (nothing is ever denied here):
- the command is one `sed` call, optionally preceded by `cd <repo> &&`; no pipes, `;`, `&&` after
  sed, redirects, `$`, or backticks outside quotes;
- options are only -i, -n, -E, -r, -e <script>;
- every script is only substitutions `s<d>pat<d>repl<d>[gI0-9]*`, or (with -n) print commands
  like `5p`, `5,20p`, `$p`. So GNU sed's `e`/`w`/`r` (run commands, write or read other files)
  can't appear;
- every file operand resolves inside the repo, and not under .git/, .claude/ (settings and hooks)
  or env/ (secrets).
"""

import json
import os
import re
import shlex
import sys

REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROTECTED = (".git", ".claude", "env")
_PRINT = re.compile(r"(\d+|\$)(,(\d+|\$))?p")


def to_native(path: str) -> str:
    """Git Bash /c/Users/... -> C:/Users/..."""
    m = re.match(r"^/([a-zA-Z])(/|$)", path)
    return f"{m.group(1)}:/{path[3:]}" if m else path


def inside_repo(path: str, cwd: str) -> bool:
    full = os.path.realpath(os.path.join(cwd, to_native(path)))
    rel = os.path.relpath(full, REPO) if os.path.splitdrive(full)[0].lower() == os.path.splitdrive(REPO)[0].lower() else ".."
    if rel == "." or rel.startswith(".."):
        return False
    return rel.replace("\\", "/").split("/")[0] not in PROTECTED


def substitution_end(script: str, i: int) -> int | None:
    """Index after one s<d>pat<d>repl<d>flags starting at script[i] == 's', or None."""
    if i + 1 >= len(script):
        return None
    delim = script[i + 1]
    if delim in "\\\n" or delim.isalnum() or delim.isspace():
        return None
    j, parts = i + 2, 0
    while parts < 2:
        if j >= len(script):
            return None
        if script[j] == "\\":
            j += 2
            continue
        if script[j] == delim:
            parts += 1
        j += 1
    while j < len(script) and (script[j] in "gI" or script[j].isdigit()):
        j += 1
    return j


def script_is_safe(script: str, print_only_ok: bool) -> bool:
    i = 0
    while i < len(script):
        if script[i] in " \t\n;":
            i += 1
            continue
        if script[i] == "s":
            end = substitution_end(script, i)
        elif print_only_ok and (m := _PRINT.match(script, i)):
            end = m.end()
        else:
            return False
        if end is None or (end < len(script) and script[end] not in " \t\n;"):
            return False
        i = end
    return True


def is_safe(command: str, cwd: str) -> bool:
    if "`" in command or "$(" in command:
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    if len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&":
        if os.path.realpath(to_native(tokens[1])) != REPO:
            return False
        cwd, tokens = REPO, tokens[3:]
    if not tokens or tokens[0] != "sed" or any(set(t) <= set(";&|<>") for t in tokens):
        return False

    scripts, files, quiet = [], [], False
    args = iter(tokens[1:])
    for arg in args:
        if arg == "-e":
            scripts.append(next(args, ""))
        elif arg.startswith("-") and len(arg) > 1:
            if any(flag not in "inEr" for flag in arg[1:]):
                return False
            quiet = quiet or "n" in arg
        elif not scripts:
            scripts.append(arg)
        else:
            files.append(arg)
    if not scripts or not files:
        return False
    if any("$" in a for a in files):
        return False
    return all(script_is_safe(s, quiet) for s in scripts) and all(inside_repo(f, cwd) for f in files)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        command = payload["tool_input"]["command"]
        cwd = payload.get("cwd") or os.getcwd()
    except (ValueError, KeyError, TypeError):
        return
    if is_safe(command, to_native(cwd)):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "sed substitution on files inside the repo",
            }
        }))


if __name__ == "__main__":
    main()
