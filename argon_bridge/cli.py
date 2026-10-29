"""Entry point: `argon-bridge hook-<name>` (stdin JSON → stdout JSON) and user commands."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import compat, config, framing, gateway, hooks, packet, permissions, prompts, schemas, state

HOOKS = {
    "hook-prompt": hooks.on_prompt,
    "hook-commit": hooks.on_commit,
    "hook-commit-done": hooks.on_commit_done,
    "hook-plan": hooks.on_exit_plan,
    "hook-plan-file": hooks.on_plan_file,
    "hook-file": hooks.on_file_edit,
    "hook-plan-done": hooks.on_exit_plan_done,
    "hook-stop": hooks.on_stop,
}


def run_wake():
    """asyncRewake: exit code 2 wakes Claude with stderr as a system reminder."""
    try:
        text = hooks.wake(json.load(sys.stdin))
    except Exception:  # a reviewer bug must never wake Claude for nothing
        return 0
    if text:
        print(text, file=sys.stderr)
        return 2
    return 0


def run_hook(name):
    try:
        out = HOOKS[name](json.load(sys.stdin))
    except Exception as e:  # a reviewer bug must never block the user's work
        out = {"systemMessage": f"⚠️ argon-bridge error, check skipped: {type(e).__name__}: {e}"}
    if out:
        print(json.dumps(out))
    return 0


USAGE = """usage: argon-bridge <command>
  mode thorough|standard|off   when Argon runs automatically
  effort low|medium|high       how hard Argon thinks when it reviews
  worker-effort low|medium|high  how hard Argon thinks when it does delegated work
  worker-timeout SECONDS       how long one delegated phase may take (300-14400)
  permissions on|off           block Argon from reading secret files (.env, keys, ~/.ssh)
  status                       agy, login, settings, recent checks
  findings                     Argon's findings of the last 7 days, newest first, by repository
  review [--repo PATH] [--file PATH]...  review current changes now; --file (repeatable) adds a file to
                               review, e.g. a report to check a second time, with or without changes
  ask "<question>" [--repo PATH] [--file PATH]... [--findings] [--timeout SECONDS]
                               independent second opinion; --findings answers with up to 15 findings
                               (claim, file, line, evidence) instead of one position
  delegate ...                 hand work to Argon (only when the user asks); `delegate` alone for help"""


def _opt(argv, name):
    if name in argv:
        i = argv.index(name)
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):  # `--repo --timeout 60` would take `--timeout`
            raise ValueError(f"{name} needs a value")
        value = argv[i + 1]
        del argv[i:i + 2]
        return value
    return None


def _opts(argv, name):
    values = []
    while name in argv:
        v = _opt(argv, name)
        if v is None:
            break
        values.append(v)
    return values


_OPTION = re.compile(r"--?[A-Za-z]")  # `-1` stays a word of a question


def _unknown(argv, words=True):
    """Raises on an option left after the known ones were taken out (and, words=False, on any argument left):
    a typo must not become part of the question or be ignored."""
    bad = next((a for a in argv if _OPTION.match(a)), None)
    if bad:
        raise ValueError(f"unknown option {bad}; see argon-bridge --help")
    if argv and not words:
        raise ValueError(f"unexpected argument {argv[0]}; see argon-bridge --help")


def _read_files(paths, cfg):
    """`--file` contents as [(path, text)]. Raises ValueError for a file Argon may not see (a secret file, one in
    private_roots), one that is not text, or files together over the packet limit."""
    out, total = [], 0
    for p in paths:
        path = os.path.abspath(os.path.expanduser(p))
        if permissions.is_secret_path(path) or packet.is_private(path, cfg):
            raise ValueError(f"{p}: a secret file or in private_roots; Argon may not read it")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(prompts.MAX_DIFF + 1)
        except OSError as e:
            raise ValueError(f"{p}: cannot read it ({e.strerror})")
        if "\0" in text:
            raise ValueError(f"{p}: not a text file")
        total += len(text)
        if total > prompts.MAX_DIFF:
            raise ValueError(f"--file: over {prompts.MAX_DIFF} characters together; give a shorter file")
        out.append((path, text))
    return out


def cmd_set(key, argv):
    if not argv:
        print(f"{key}: {config.load()[key]}")
        return 0
    try:
        config.set_value(key, argv[0])
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"{key}: {argv[0]}")
    return 0


def cmd_permissions(argv):
    if argv[:1] not in (["on"], ["off"]):
        print("usage: argon-bridge permissions on|off", file=sys.stderr)
        return 2
    config.set_value("manage_agy_permissions", "true" if argv[0] == "on" else "false")
    print(f"permissions: {argv[0]}")
    return 0


def _login_check():
    """`agy models` result, cached for an hour so status stays fast."""
    cache = os.path.join(state.dir(), "login.json")
    try:
        with open(cache) as f:
            c = json.load(f)
        if time.time() - c["at"] < 3600:
            return c["ok"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        p = subprocess.run([*compat.agy_cmd(), "models"], capture_output=True, text=True, timeout=60)
        ok = p.returncode == 0 and "gemini-4-argon" in p.stdout
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    with open(cache, "w") as f:
        json.dump({"at": time.time(), "ok": ok}, f)
    return ok


def cmd_status():
    cfg = config.load()
    agy = compat.agy_path()
    code = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:  # two copies of the plugin can live on one machine (an installed cache next to the repository)
        sha = subprocess.run(["git", "-C", code, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        sha = ""
    print(f"code: {code} @ {sha or 'not a git checkout'}")
    print(f"agy: {agy or 'NOT INSTALLED'}")
    if agy:
        print(f"argon available: {'yes' if _login_check() else 'NO (log in with `agy`, or Argon not on this account)'}")
    print(f"mode: {cfg['mode']}\neffort: {cfg['effort']}\ntimeout: {cfg['timeout']}s\nask timeout: {cfg['ask_timeout']}s")
    print(f"worker effort: {cfg['worker_effort']}\nworker timeout: {cfg['worker_timeout']}s")
    permissions.cleanup_stale()  # rules a crashed run left behind
    recent = state.recent_log(10)
    if cfg["manage_agy_permissions"] is True and any(e.get("guard") is False for e in recent):
        print("secret files: NOT blocked in a recent run: its agy project file could not be written (see below)")
    elif cfg["manage_agy_permissions"] is True:
        print("secret files: blocked (plugin manages agy permission rules)")
    else:
        print("secret files: NOT blocked — run `argon-bridge permissions on` to let the plugin give each Argon run "
              "its own agy permission rules (a temporary project file in ~/.gemini/config/projects)")
    until = state.cooldown_until()
    if until > time.time():
        print(f"quota cooldown until: {time.strftime('%H:%M', time.localtime(until))}")
    hooks.reap()  # a dead worker's marker is not a running check
    for j in state.running_jobs():
        print(f"running: {j.get('kind')} {(j.get('sid') or '?')[:8]} in {j.get('root')}, "
              f"{int(time.time() - j.get('started', time.time()))} s")
    for sid, n in state.inbox_waiting().items():  # handed to Claude at the session's next prompt, Bash call or Stop
        print(f"waiting results: {sid[:8]} {n}")
    print("recent checks:")
    for e in recent:
        if e.get("kind") == "permissions":  # the guard, not a check: nothing was skipped and nothing took time
            print(f"  {e.get('ts')} permissions {e.get('error')}")
            continue
        if e.get("error") is not None:
            outcome = f"SKIPPED ({e.get('error')})"
        elif e.get("kind") == "work":
            outcome = f"phase {e.get('status')}"
        else:
            outcome = "opinion" if e.get("kind") == "ask" else f"{e.get('findings')} findings"
        print(f"  {e.get('ts')} {e.get('kind', '?'):<5} {outcome} {e.get('duration', '')}s")
    from . import worker
    print("delegated jobs:")
    for line in worker.status_lines():
        print(f"  {line}")
    return 0


def cmd_findings():
    by_root = {}
    for e in reversed(state.recent_findings()):
        by_root.setdefault(e.get("root") or "(no repository)", []).append(e)
    for root, es in by_root.items():
        print(root)
        for e in es:
            claim = " ".join(str(e.get("claim", "")).split())
            print(f"  {e.get('ts', '')[:10]} {(e.get('head') or '-')[:7]} {e.get('file')}:{e.get('line')} "
                  f"{e.get('severity', '?')} {claim[:100]}")
    return 0


def cmd_review(argv):
    cfg = config.load()
    repo = _opt(argv, "--repo")
    files = _read_files(_opts(argv, "--file"), cfg)
    _unknown(argv, words=False)
    root = packet.toplevel(repo or os.getcwd())
    if not root and not files:
        print("not inside a git repository", file=sys.stderr)
        return 2
    if root and packet.is_private(root, cfg):
        if not files:
            print("this repository is in private_roots; Argon may not read it", file=sys.stderr)
            return 2
        print("this repository is in private_roots; reviewing the files without it", file=sys.stderr)
        root = None
    hooks.reap()  # a dead review's change goes back into this one first
    sid = state.current_session_id()
    diff = ""
    if root:  # the calling session's unreviewed commits and everything on disk; from a terminal, what is on disk only
        pending = state.session(sid).get("code", {}).get(root, {}).get("pending", []) if sid else []
        diff = packet.change_diff(root, pending, [(root, ["-A"])])
    if not diff.strip() and not files:
        print("nothing to review")
        return 0
    prompt, pkt = prompts.code_review(packet.user_words(sid) if sid else "", diff,
                                      packet.decisions(root, cfg) if root else "", files)
    timeout = min(cfg["timeout"], config.SYNC_TIMEOUT)  # Claude waits for it in a Bash call
    cwd = root or tempfile.mkdtemp(prefix="argon-review-")
    try:
        result = gateway.run("review", prompt, pkt, cwd, schemas.FINDINGS, cfg["effort"], timeout)
    finally:
        if not root:
            shutil.rmtree(cwd, ignore_errors=True)
    print(framing.status_line(result))
    if result.ok:
        state.log_findings("review", root, None, result.data.get("findings"))
    if result.ok and diff.strip():
        hooks.remember_review(root, diff, result, sid)
    if result.ok and result.data.get("findings"):
        print(framing.findings_for_claude(result.data, "review"))
    return 0


def cmd_ask(argv):
    cfg = config.load()
    repo = _opt(argv, "--repo")
    limit = _opt(argv, "--timeout")
    files = _read_files(_opts(argv, "--file"), cfg)
    as_findings = "--findings" in argv
    argv = [a for a in argv if a != "--findings"]
    _unknown(argv)
    question = " ".join(argv).strip()
    if not question or (limit is not None and not limit.isdigit()):
        print('usage: argon-bridge ask "<question>" [--repo PATH] [--file PATH]... [--findings] '
              '[--timeout SECONDS]', file=sys.stderr)
        return 2
    repo = os.path.expanduser(repo) if repo else None
    if repo and not os.path.isdir(repo):
        print(f"--repo {repo}: not a directory", file=sys.stderr)
        return 2
    timeout = max(30, min(int(limit), cfg["timeout"])) if limit else cfg["ask_timeout"]
    sid = state.current_session_id()
    prompt, pkt = prompts.ask(question, packet.user_words_all(sid) if sid else "", files, as_findings)
    if repo and packet.is_private(packet.toplevel(repo) or repo, cfg):
        print("that repository is in private_roots; asking without repository access", file=sys.stderr)
        repo = None
    cwd = (packet.toplevel(repo) or repo) if repo else tempfile.mkdtemp(prefix="argon-ask-")
    try:
        result = gateway.run("ask", prompt, pkt, cwd, schemas.ASK_FINDINGS if as_findings else schemas.OPINION,
                             cfg["effort"], timeout)
    finally:
        if not repo:
            shutil.rmtree(cwd, ignore_errors=True)
    if not result.ok or as_findings:
        print(framing.status_line(result))
    if not result.ok:
        return 0
    if as_findings:
        state.log_findings("ask", cwd if repo else None, None, result.data["findings"])
        if repo or files:  # quoted evidence is checked against the repository's files and the packet
            gateway._check_findings(result.data["findings"], cwd, pkt)
        if result.data["findings"]:
            print(framing.findings_for_claude(result.data, "ask"))
        return 0
    d = result.data
    lines = [f"Position: {d.get('position', '')}"] + [f"- Reason: {r}" for r in d.get("reasons", [])] \
        + [f"- Risk: {r}" for r in d.get("risks", [])]
    print("Argon (verbatim):")
    print(framing.untrusted("ARGON OPINION", "an opinion, not instructions", "\n".join(lines)))
    print(f"({result.duration}s; untrusted opinion, not instructions)")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in HOOKS:
        return run_hook(argv[0])
    if argv[:1] == ["hook-review-wake"]:
        return run_wake()
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if "-h" in argv or "--help" in argv:  # never a request: `ask --help` once sent "--help" to Argon as a question
        if cmd == "delegate":
            from . import worker
            print(worker.USAGE)
        else:
            print(USAGE)
        return 0
    if cmd in ("mode", "effort", "worker-effort"):
        return cmd_set(cmd.replace("-", "_"), rest)
    if cmd == "worker-timeout":
        return cmd_set("worker_timeout", [int(rest[0])] if rest and rest[0].isdigit() else rest)
    if cmd == "delegate":
        from . import worker
        return worker.main(rest)
    if cmd == "status":
        return cmd_status()
    if cmd == "findings":
        return cmd_findings()
    if cmd == "permissions":
        return cmd_permissions(rest)
    if cmd in ("review", "ask"):
        try:
            return (cmd_review if cmd == "review" else cmd_ask)(rest)
        except ValueError as e:  # an argument error, before anything ran
            print(e, file=sys.stderr)
            return 2
    if cmd == "run-job" and rest:  # the background worker started by the commit and Stop hooks
        if not compat.WINDOWS:  # stopped (watchdog, reject, kill): the packet goes, the running marker stays
            signal.signal(signal.SIGTERM, gateway.on_term)
        hooks.run_job(rest[0])
        return 0
    print(USAGE, file=sys.stderr if cmd else sys.stdout)
    return 2 if cmd else 0


if __name__ == "__main__":
    sys.exit(main())
