"""Claude Code hook handlers. Each takes the hook's stdin dict and returns its stdout dict ({} = no-op).
Rule: never block work because Argon failed; block only to hand findings to Claude.

Parallel sessions may share a checkout, so a session reviews only its own change: commits it owns (the
repository's ledger, state.owners) and files it edited (`touched`). Its review state is per session and repository
(session["code"][root]: `pending` = owned commits not reviewed yet, `pre` = its commit command in progress)."""
import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import compat, config, framing, gateway, packet, permissions, prompts, schemas, state

FIX_PASS_PROMPTS = 3  # a fix pass expires after this many new user prompts


def _head(root):
    p = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def _sub(inp):
    return bool(inp.get("agent_id"))


def _digest(text):
    return hashlib.sha1(text.encode()).hexdigest()


def _review(kind, root, prompt, pkt, cfg):
    """Runs Argon in `root`, or in an empty dir when root is None (no repository access). A commit review runs in a
    snapshot of the reviewed commit: Claude goes on editing `root` during the 2-8 minutes Argon reads it."""
    work = None if root else tempfile.mkdtemp(prefix="argon-bridge-norepo-")
    snap = _snapshot(root, cfg.get("head")) if kind == "code" and root else None
    meta = {k: cfg.get(k) for k in ("sid", "root", "base", "head", "shas")} if kind != "plan" else None
    # the user's own secret files too: the packet (their words) may name paths in `root`
    rules = {"deny": list(dict.fromkeys(permissions.secret_rules(snap) + permissions.secret_rules(root)))} \
        if snap else None
    try:
        result = gateway.run(kind, prompt, pkt, snap or root or work, schemas.FINDINGS, cfg["effort"],
                             cfg["timeout"], rules=rules, meta=meta)
        for f in (result.data or {}).get("findings", []) if snap and result.ok else []:
            path = os.path.realpath(os.path.join(snap, f["file"]))
            if path.startswith(os.path.realpath(snap) + os.sep):  # a snapshot path means nothing once it is gone
                f["file"] = os.path.relpath(path, os.path.realpath(snap)).replace(os.sep, "/")
        return result
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        if snap:
            _drop_snapshot(root, snap)


def _snapshots():
    return os.path.join(state.dir(), "snapshots")


def _snapshot(root, head):
    """A detached worktree of `head` under the state dir, or None (the review then reads the live tree).
    Sweeps the snapshots of workers killed before they removed theirs (SIGTERM: gateway.on_term exits at once)."""
    if not head:
        return None
    no_hooks = os.path.join(_snapshots(), ".no-hooks")  # an empty dir: no post-checkout hook (husky) runs
    os.makedirs(no_hooks, exist_ok=True)
    for n in os.listdir(_snapshots()):
        p = os.path.join(_snapshots(), n)
        with contextlib.suppress(OSError):
            if not n.startswith(".") and time.time() - os.path.getmtime(p) > gateway.TEMP_AGE:
                shutil.rmtree(p, ignore_errors=True)  # its worktree entry goes with the next prune of its repo
    snap = os.path.join(_snapshots(), state._stamp())
    p = subprocess.run(["git", "-C", root, "-c", f"core.hooksPath={no_hooks}", "worktree", "add", "-q", "--detach",
                        snap, head], capture_output=True, text=True,
                       env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"})  # no LFS downloads for a read-only copy
    if p.returncode != 0:
        _drop_snapshot(root, snap)
        return None
    return snap


def _drop_snapshot(root, snap):
    for args in (("worktree", "remove", "--force", snap), ("worktree", "prune")):
        subprocess.run(["git", "-C", root, *args], capture_output=True)
    shutil.rmtree(snap, ignore_errors=True)


def _start_review(sid, kind, root, prompt, pkt, cfg, shas):
    """Code reviews run in a detached worker so the commit or the turn does not wait for Argon (2-8 min).
    The result lands in the session's inbox and reaches Claude at the next prompt, Bash call or Stop.
    shas: the session's commits in the review, given back to it if the review fails or dies."""
    spans = packet.runs(root, shas)
    start_background({"sid": sid, "kind": kind, "root": root, "prompt": prompt, "packet": pkt,
                      "effort": cfg["effort"], "timeout": cfg["timeout"], "shas": shas,
                      "base": spans[0][0] if spans else None, "head": shas[-1] if shas else None}, root)


def _marker(spec):
    """What a running marker records about a background job (state.mark_running)."""
    if spec.get("type") == "phase":
        return {**{k: spec.get(k) for k in ("sid", "root", "timeout", "job_id")}, "kind": "phase"}
    return {k: spec.get(k) for k in ("sid", "kind", "root", "timeout", "shas", "handoff", "digest")}


def start_background(spec, cwd):
    """Runs `spec` (a review, or a delegated phase: "type": "phase") in a detached `run-job` worker."""
    path = state.new_job(spec)
    if os.environ.get("ARGON_REVIEWER_FOREGROUND"):  # tests
        return run_job(path)
    # the worker runs in the user's repo, so it is told where this package lives (the launcher sets no PYTHONPATH)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [root, os.environ.get("PYTHONPATH")]))}
    p = subprocess.Popen([sys.executable, "-m", "argon_bridge.cli", "run-job", path], cwd=cwd, env=env,
                         stdin=subprocess.DEVNULL, **compat.detached(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # marked here too, so a worker that dies before it marks itself (an import error) is still reaped; the
    # worker needs far longer to start than this write takes, then marks itself again with the same pid
    state.mark_running(os.path.basename(path)[:-5], _marker(spec), pid=p.pid)


def _run_phase(job_id, job):
    from . import worker  # worker imports hooks
    state.mark_running(job_id, _marker(job))
    try:
        worker.run_phase(job["job_id"])
    except Exception:  # run_phase reports its own errors; this is a crash around them (disk, state files)
        worker.mark_dead({"job_id": job["job_id"]})
    finally:
        state.clear_running(job_id)


def run_job(path):
    with open(path) as f:
        job = json.load(f)
    os.remove(path)
    job_id = os.path.basename(path)[:-5]
    if not os.environ.get("ARGON_REVIEWER_FOREGROUND"):  # tests run this inline, in their own process
        # state.reap_dead calls it stale at + 120; agy runs in a session of its own (gateway._stream)
        compat.watchdog(2 * job.get("timeout", 900) + 60, first=lambda: compat.stop_agy(gateway.RUNNING_AGY))
    if job.get("type") == "phase":
        return _run_phase(job_id, job)
    state.mark_running(job_id, _marker(job))
    try:
        result = _review(job["kind"], job["root"], job["prompt"], job["packet"], job)
    except Exception as e:  # the session must still hear that the check did not happen
        result = gateway.Result(False, error="agy_error", detail=f"argon-bridge error: {type(e).__name__}: {e}")
    finally:
        state.clear_running(job_id)
    handoff = job.get("handoff")
    if handoff:  # a plan review: the hook may still be waiting for it
        res = os.path.join(handoff, "result.json")
        state._write(res, {"ok": result.ok, "data": result.data, "error": result.error,
                           "detail": result.detail, "duration": result.duration})
        if _hook_waits(handoff) or not _claim(res):
            return  # the hook takes it
        shutil.rmtree(handoff, ignore_errors=True)
    if not result.ok:  # not reviewed: undone here, not when a hook takes the note, so no review is skipped meanwhile
        if handoff:
            _forget_plan(job["sid"], job.get("digest"))
        else:
            _unreviewed(job)  # as for a dead review: the change goes into the next one
    findings = (result.data or {}).get("findings") if result.ok else None
    state.inbox_put(job["sid"], {"kind": "plan_late" if handoff else job["kind"], "root": job["root"],
                                 "status": framing.status_line(result), "findings": findings,
                                 "digest": job.get("digest"), "ok": result.ok, "shas": job.get("shas"),
                                 "head": job.get("head"), "range": f"{job['base']}..{job['head']}" if job.get("base") else None})


def _hook_waits(handoff):
    """Whether the hook that started this plan review still waits for it (a killed hook leaves its marker)."""
    waiting = os.path.join(handoff, "waiting")
    try:
        with open(waiting) as f:
            pid = f.read().strip()
    except OSError:
        return False
    if pid and compat.pid_alive(pid):
        return True
    with contextlib.suppress(OSError):
        os.remove(waiting)
    return False


def _claim(path):
    """Takes a handed-over result once: of the waiting hook and the worker, exactly one wins the rename."""
    try:
        os.rename(path, path + ".taken")
        return True
    except OSError:
        return False


def _plan_wait(spec, cwd, wait):
    """Runs a plan review in the background and waits up to `wait` seconds for it. Returns the Result, or
    None when the review goes on after that: the worker then hands it to the session's inbox."""
    handoff = os.path.join(state.dir(), "handoff", f"{os.getpid()}-{time.time_ns()}")
    os.makedirs(handoff)
    waiting, res = os.path.join(handoff, "waiting"), os.path.join(handoff, "result.json")
    with open(waiting, "w") as f:
        f.write(str(os.getpid()))
    start_background({**spec, "handoff": handoff}, cwd)
    deadline = time.time() + wait
    while not (os.path.exists(res) and _claim(res)):
        if time.time() >= deadline:
            with contextlib.suppress(OSError):  # gone when reap removed the handoff of a dead review
                os.remove(waiting)
            if os.path.exists(res) and _claim(res):  # it landed just now
                break
            return None
        time.sleep(0.5)
    with open(res + ".taken") as f:
        r = json.load(f)
    shutil.rmtree(handoff, ignore_errors=True)
    return gateway.Result(ok=r["ok"], data=r["data"], error=r["error"], detail=r["detail"], duration=r["duration"])


DIED = ("⚠️ Argon background check died (process gone or far past its time limit); this change was not "
        "reviewed and goes into the next review")


PLAN_DIED = "⚠️ Argon's plan review died (process gone or far past its time limit); the plan was not reviewed"


DELEGATE = "argon-bridge delegate"  # every delegated-phase note names the next delegate command


def _unreviewed(m):
    """A failed or dead job's change was never reviewed: its commits go back to its own session (never another
    one's), ahead of that session's later ones, into its next review."""
    if not m.get("sid"):
        return
    data = state.session(m["sid"])
    if m.get("kind") == "code" or (m.get("kind") == "stop" and m.get("shas")):
        code = data.setdefault("code", {}).setdefault(m["root"], {})
        code["pending"] = list(dict.fromkeys((m.get("shas") or []) + code.get("pending", [])))
    if m.get("kind") != "code":
        data.pop("stop_digest", None)
    state.save_session(m["sid"], data)


def reap():
    """A dead background worker leaves no result; its session must not wait forever."""
    for m in state.reap_dead():
        if m.get("kind") == "phase":
            from . import worker
            worker.mark_dead(m)
            continue
        if m.get("kind") == "plan":
            if m.get("handoff"):
                shutil.rmtree(m["handoff"], ignore_errors=True)
            _forget_plan(m["sid"], m.get("digest"))  # never reviewed: the same plan text is reviewed again
            state.inbox_put(m["sid"], {"kind": "plan_late", "root": m["root"], "findings": None, "status": PLAN_DIED})
            continue
        _unreviewed(m)
        state.inbox_put(m["sid"], {"kind": m["kind"], "root": m["root"], "findings": None, "status": DIED,
                                   "shas": m.get("shas")})


def _forget_plan(sid, digest):
    data = state.session(sid)
    if digest and digest in data.get("plan_digests", []):
        data["plan_digests"].remove(digest)
        state.save_session(sid, data)


MAX_CONTEXT = 12000  # characters handed to Claude at once; a 25 KB hook output was moved to a file by Claude Code


def _commits(shas, done):
    n = len(shas)
    return f"{n} commit{'s' * (n != 1)} {'reviewed' if done else 'not reviewed'}: {' '.join(s[:7] for s in shas)}"


def _inbox(sid):
    """(text for Claude, whether it has findings) for the background results waiting for this session."""
    parts, has_findings, groups, roots = [], False, [], []
    reap()
    for item in state.inbox_take(sid):
        if item.get("kind") == "plan_late":
            data = state.session(sid)
            if item.get("digest") and item.get("ok") is not False \
                    and item["digest"] not in data.get("plan_digests", []):  # the same plan text is not reviewed again
                data["plan_digests"] = (data.get("plan_digests", []) + [item["digest"]])[-20:]
                state.save_session(sid, data)
            if item.get("findings"):
                has_findings = True
                _set_fix_pass(sid, "plan_fix_pass")  # the revised plan is not reviewed again
                parts.append(framing.findings_for_claude({"findings": item["findings"]}, "plan_late"))
            else:
                parts.append(f"argon-bridge (plan review that finished after the plan went ahead): {item['status']}")
            continue
        if item.get("kind") == "phase":
            parts.append(item["status"])
            continue
        roots += [item["root"]] if item["root"] not in roots else []
        shas = item.get("shas") or []
        done = item.get("ok", item.get("findings") is not None)
        where = item["root"] + (f", {_commits(shas, done)}" if shas else "")
        if item.get("findings"):
            has_findings = True
            if item["kind"] == "code" and item.get("head"):  # Argon read a snapshot of head (_review)
                _mark_changed(item["root"], item["head"], item["findings"])
            _set_fix_pass(sid, "code_fix_pass", item["root"])  # the fix commit is not reviewed again
            label = " ".join(s[:7] for s in shas) or "uncommitted changes"
            groups.append((f"review of {where}" if shas else f"review of uncommitted changes in {item['root']}",
                           label, item["findings"], item["kind"]))
        else:
            parts.append(f"argon-bridge (background check, {where}): {item['status']}")
    if groups:
        parts.append(_code_findings(sid, groups))
    code = state.session(sid).get("code", {})
    for root in roots:
        n = len(code.get(root, {}).get("pending", []))
        if n:
            parts.append(f"argon-bridge ({root}): {n} commit{'s' * (n != 1)} not reviewed yet "
                         "(will be in the next review)")
    return "\n\n".join(parts), has_findings


def _mark_changed(root, head, findings):
    """Findings on files that differ now from the reviewed commit: their lines may have moved."""
    changed = set((packet._git(root, "diff", "--name-only", "-z", head, "--") or "").split("\0"))  # -z: no quoting
    for f in findings:
        if os.path.normpath(f["file"]).replace(os.sep, "/") in changed:
            f["changed_since_review"] = True


def _code_findings(sid, groups):
    """Background code review findings, one section per review and each finding once, cut to MAX_CONTEXT."""
    kind = "async" if any(g[3] == "code" for g in groups) else "stop"
    merged = framing.grouped_findings([g[:3] for g in groups])

    def render(gs):
        return framing.findings_for_claude({"findings": [f for _, fs in gs for f in fs]}, kind,
                                           framing.grouped_body(gs))
    text = render(merged)
    if len(text) <= MAX_CONTEXT:
        return text
    path = os.path.join(state.dir(), "sessions", f"{sid}-findings-{time.time_ns()}.txt")  # pruned with the session
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    flat = [(h, f) for h, fs in merged for f in fs]

    def cut(k):
        gs = []
        for h, f in flat[:k]:
            if gs and gs[-1][0] == h:
                gs[-1][1].append(f)
            else:
                gs.append((h, [f]))
        return render(gs) + f"\n{len(flat) - k} more findings in {path}"
    k = len(flat) - 1
    while k > 0 and len(cut(k)) > MAX_CONTEXT:
        k -= 1
    return cut(k)


def remember_review(root, diff, result, sid=None):
    """Keeps a manual `review` result so a commit of the same diff does not ask Argon again;
    with findings, the fix that follows is not reviewed again either."""
    repo = state.repo(root)
    repo["last_review"] = {"digest": _digest(diff), "data": result.data, "duration": result.duration}
    state.save_repo(root, repo)
    if sid and result.data.get("findings"):
        _set_fix_pass(sid, "code_fix_pass", root)


def _reused(repo, diff):
    # shortcut: keyed on the diff only; the user's words or effort may have changed since, which is fine for one change
    last = repo.get("last_review") or {}
    if last.get("digest") != _digest(diff):
        return None
    return gateway.Result(ok=True, data=last["data"], duration=last["duration"],
                          detail="already handed to Claude by the last `review`")


def _has_fix_pass(sid, key, root=None):
    """True after findings were handed to Claude, so the fix is not reviewed again."""
    data = state.session(sid)
    p = data.get(key)
    if not isinstance(p, dict) or p.get("root") != root:
        return False
    return data.get("prompt_count", 0) - p["at"] <= FIX_PASS_PROMPTS


def _clear_fix_pass(sid, key):
    data = state.session(sid)
    if data.pop(key, None) is not None:
        state.save_session(sid, data)


def _set_fix_pass(sid, key, root=None):
    data = state.session(sid)
    data[key] = {"at": data.get("prompt_count", 0), "root": root}
    state.save_session(sid, data)


def _deny(result, kind):
    return {
        "systemMessage": framing.status_line(result),
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": framing.findings_for_claude(result.data, kind),
        },
    }


def _tell_claude(status):
    """PostToolUse output that puts a status into Claude's context."""
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": f"argon-bridge (automatic check): {status}"}}


# the plugin's own commands, and what the harness sends as a prompt (background task results carry Claude's work)
NOT_USER_WORDS = ("/argon-bridge", "<task-notification>", "<system-reminder>", "<bash-", "<local-command-")


def on_prompt(inp):
    if _sub(inp):
        return {}
    text = (inp.get("prompt") or "").strip()
    if text and not text.startswith(NOT_USER_WORDS):
        state.append_prompt(inp["session_id"], text)
    waiting, _ = _inbox(inp["session_id"])
    if not waiting:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": waiting}}


def on_commit(inp):
    """Never blocks and starts no review: notes where HEAD is and what the command will stage, so PostToolUse can
    tell this session's new commits from a parallel session's (_own)."""
    command = (inp.get("tool_input") or {}).get("command", "")
    cfg = config.load()
    if _sub(inp) or cfg["mode"] == "off" or not packet.is_commit(command):
        return {}
    root = packet.target_repo(command, inp.get("cwd", "."))
    if not root or packet.is_private(root, cfg):
        return {}
    sid = inp["session_id"]
    reap()  # a dead review's commits go back to their session before this one is read
    # PreToolUse runs before `git add` in `git add … && git commit`, so stage what it would, in a throwaway index
    adds = packet.staging(command, inp.get("cwd", "."), root, every=True)
    head = _head(root)
    pre = {"head": head, "paths": packet.code_diff(root, None, adds, names=True).split("\n")[:-1], "t": time.time()}
    data = state.session(sid)
    code = data.setdefault("code", {}).setdefault(root, {})
    old = code.get("pre")
    if old and pre["t"] - old["t"] < STALE_COMMIT:  # its PostToolUse never ran: its commits are still to claim
        pre.update(head=old["head"], paths=sorted(set(old["paths"]) | set(pre["paths"])))
    with state.owners(root) as o:
        o["inflight"][sid] = pre
        if "pending" not in code:
            code["pending"] = _seed(root, head, o["ledger"], sid)
    code["pre"] = pre
    state.save_session(sid, data)
    return {}


LEDGER_SIZE = 2000  # commits remembered per repository; far more than one review cycle
STALE_COMMIT = 3600  # an inflight commit command older than this lost its PostToolUse


def _seed(root, head, ledger, sid):
    """A session's first commit in a repository reviewed before per-session state: it takes the old shared
    baseline, once, so commits it left unreviewed are not lost."""
    repo = state.repo(root)
    legacy = repo.pop("last_reviewed_head", None)
    if not legacy or not head:
        return []
    state.save_repo(root, repo)
    span = head if legacy == packet.EMPTY_TREE else f"{legacy}..{head}"
    shas = [s for s in (packet._git(root, "rev-list", "--first-parent", "--reverse", "--max-count=200", span)
                        or "").split() if s not in ledger]
    ledger.update({s: {"sid": sid, "t": time.time()} for s in shas})
    return shas


def _own(sid, root, pre, head):
    """The commits since this session's PreToolUse that it made. Another session's commit can land in the same
    window: a commit no other running commit command could have made is ours; otherwise it is ours when it changes
    a path our command stages (with no paths known, ours, marked ambiguous) and left to the other session if not.
    The ledger keeps each commit with one session."""
    span = f"{pre['head']}..{head}" if pre.get("head") else head
    shas = (packet._git(root, "rev-list", "--first-parent", "--reverse", span) or "").split()
    mine, now, owned = set(pre.get("paths") or []), time.time(), []
    with state.owners(root) as o:
        o["inflight"] = {k: v for k, v in o["inflight"].items() if k != sid and now - v.get("t", 0) < STALE_COMMIT}
        for sha in shas:
            if sha in o["ledger"]:
                continue
            rivals = [v for v in o["inflight"].values() if not v.get("head") or packet._git(
                root, "merge-base", "--is-ancestor", sha, v["head"]) is None]  # the commit landed after its start
            entry = {"sid": sid, "t": now}
            if rivals:
                files = set(packet.commit_files(root, packet.runs(root, [sha])[0][0], sha))
                if not mine:
                    entry["ambiguous"] = True
                elif not files & mine:
                    continue
                elif any(files & set(v.get("paths") or []) for v in rivals):
                    entry["ambiguous"] = True
            o["ledger"][sha] = entry
            owned.append(sha)
        if len(o["ledger"]) > LEDGER_SIZE:
            o["ledger"] = dict(sorted(o["ledger"].items(), key=lambda kv: kv[1].get("t", 0))[-LEDGER_SIZE:])
    return owned


def _commit_landed(sid, root):
    """Claims the session's new commits and starts the review of its unreviewed ones; returns a note or None."""
    data = state.session(sid)
    code = data.get("code", {}).get(root)
    pre = code.pop("pre", None) if code else None
    if not pre:
        return None
    head = _head(root)
    owned = _own(sid, root, pre, head) if head and head != pre.get("head") else []
    if not owned:  # the commit failed, or every new commit was another session's
        with state.owners(root) as o:
            o["inflight"].pop(sid, None)
        state.save_session(sid, data)
        return None
    shas, review = code.get("pending", []) + owned, None
    spans = packet.runs(root, owned)
    done = {f for base, last in spans for f in packet.commit_files(root, base, last)}
    touched = data.get("touched", {})
    if touched.get(root):
        touched[root] = [p for p in touched[root] if p not in done]
    fixed = _has_fix_pass(sid, "code_fix_pass", root)
    diff = "" if fixed else packet.change_diff(root, shas)
    if fixed or not packet.is_small(diff):  # a small change is kept and adds up into the next review
        # not again when `review` already handed it to Claude
        review = None if fixed or _reused(state.repo(root), diff) else (diff, shas)
        shas = []
    code["pending"] = shas
    state.save_session(sid, data)
    if fixed:
        _clear_fix_pass(sid, "code_fix_pass")  # used up here, so a failed attempt keeps it
    if not review:
        return None
    cfg = config.load()
    prompt, pkt = prompts.code_review(packet.user_words(sid), review[0], packet.decisions(root, cfg))
    packet.mark_reviewed(sid)
    _start_review(sid, "code", root, prompt, pkt, cfg, review[1])
    return ("argon-bridge: Argon is reviewing this commit in the background; "
            "its findings will reach you later; fixes need the user's approval.")


def on_commit_done(inp):
    """After a commit lands, reviews the session's own new commits; also hands over waiting background results."""
    if _sub(inp):
        return {}
    sid = inp["session_id"]
    command = (inp.get("tool_input") or {}).get("command", "")
    root = packet.target_repo(command, inp.get("cwd", ".")) if packet.is_commit(command) else None
    started = _commit_landed(sid, root) if root else None
    notes = [n for n in (started, _inbox(sid)[0]) if n]
    if not notes:
        return {}
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(notes)}}
    return {"systemMessage": "Argon is reviewing this commit in the background", **out} if started else out


def on_file_edit(inp):
    """Notes the files the session edits, so its Stop review reads only its own uncommitted change."""
    ti = inp.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    if not path or config.load()["mode"] == "off":
        return {}
    path = os.path.realpath(os.path.join(inp.get("cwd", "."), path))
    root = packet.toplevel(os.path.dirname(path))
    rel = os.path.relpath(path, os.path.realpath(root)).replace(os.sep, "/") if root else ".."
    if rel.startswith(".."):
        return {}
    data = state.session(inp["session_id"])
    touched = data.setdefault("touched", {}).setdefault(root, [])
    if rel not in touched:
        touched.append(rel)
        state.save_session(inp["session_id"], data)
    return {}


def _plan_review(inp, plan_text, near):
    """near: a path inside the repository the plan is about."""
    cfg = config.load()
    if _sub(inp) or cfg["mode"] == "off" or not plan_text.strip():
        return None
    sid = inp["session_id"]
    reap()  # a plan review that died is forgotten first, so this plan is not skipped as reviewed
    data = state.session(sid)
    digest = _digest(plan_text)
    if digest in data.get("plan_digests", []):
        return None
    passed = _has_fix_pass(sid, "plan_fix_pass")
    _clear_fix_pass(sid, "plan_fix_pass")  # plans have no "landed" event: the pass is used on first use
    if passed:
        return None
    root = packet.toplevel(near)
    if root and packet.is_private(root, cfg):
        root = None  # private repo: review the plan text only, no repository access
    prompt, pkt = prompts.plan_review(packet.user_words(sid), plan_text,
                                      packet.decisions(root, cfg) if root else "")
    spec = {"sid": sid, "kind": "plan", "root": root, "prompt": prompt, "packet": pkt, "effort": cfg["effort"],
            "timeout": cfg["timeout"], "digest": digest}
    cwd = root or (near if os.path.isdir(near) else tempfile.gettempdir())
    # recorded before the review starts, so a failure or death that forgets it can never come first
    data = state.session(sid)
    data["plan_digests"] = (data.get("plan_digests", []) + [digest])[-20:]
    state.save_session(sid, data)
    result = _plan_wait(spec, cwd, cfg["plan_wait"])
    if result is None:  # still running: the same plan text is not reviewed a second time meanwhile
        return gateway.Result(False, error="background", detail=f"over {cfg['plan_wait']} s")
    if not result.ok:
        _forget_plan(sid, digest)
    elif result.data.get("findings"):
        _set_fix_pass(sid, "plan_fix_pass")
    return result


def _plan_status(sid, msg=None):
    """Kept between ExitPlanMode's PreToolUse and PostToolUse; msg=None clears it and returns the old one."""
    data = state.session(sid)
    old = data.pop("plan_status", None)
    if msg:
        data["plan_status"] = msg
    if old is not None or msg:
        state.save_session(sid, data)
    return old


def on_exit_plan(inp):
    if not _sub(inp):
        _plan_status(inp["session_id"])
    ti = inp.get("tool_input") or {}
    plan = ti.get("plan") or ""
    if not plan and ti.get("planFilePath"):
        try:
            with open(ti["planFilePath"]) as f:
                plan = f.read()
        except OSError:
            pass
    result = _plan_review(inp, plan, inp.get("cwd", "."))
    if result is None:
        return {}
    if result.ok and result.data.get("findings"):
        return _deny(result, "plan")
    msg = framing.status_line(result)
    _plan_status(inp["session_id"], msg)
    return {"systemMessage": msg}


def on_exit_plan_done(inp):
    """systemMessage is user-only, so a plan review that passed or was skipped is told to Claude here, together
    with any background result that landed while the user was approving the plan."""
    if _sub(inp):
        return {}
    status = _plan_status(inp["session_id"])
    waiting, _ = _inbox(inp["session_id"])
    if waiting and status and status.startswith(framing.STILL_REVIEWING):
        status = None  # the review it announced has already landed
    text = "\n\n".join(s for s in (status, waiting) if s)
    return _tell_claude(text) if text else {}


def on_plan_file(inp):
    ti = inp.get("tool_input") or {}
    path = ti.get("file_path", "")
    pattern = config.load()["plan_glob"].replace("**/", "*/")
    if not fnmatch.fnmatch(path, pattern):
        return {}
    result = _plan_review(inp, ti.get("content", ""), os.path.dirname(path))
    if result is None:
        return {}
    if result.ok and result.data.get("findings"):
        return {"decision": "block", "reason": framing.findings_for_claude(result.data, "plan"),
                "systemMessage": framing.status_line(result)}
    msg = framing.status_line(result)
    return {"systemMessage": msg, **_tell_claude(msg)}


def on_stop(inp):
    if _sub(inp):
        return {}
    sid = inp["session_id"]
    if inp.get("stop_hook_active"):
        return {}
    waiting, has_findings = _inbox(sid)
    if has_findings:
        return {"decision": "block", "reason": waiting, "systemMessage": "Argon's background review has findings"}
    if DIED in waiting or PLAN_DIED in waiting:  # a systemMessage reaches only the user; Claude must know
        return {"decision": "block", "reason": waiting, "systemMessage": "An Argon background check died"}
    if DELEGATE in waiting:  # a delegated job moves on without the user: Claude checks the phase now
        return {"decision": "block", "reason": waiting, "systemMessage": "Argon finished a delegated phase"}
    started = _stop_review(sid, inp.get("cwd", "."))
    msg = "\n".join(m for m in (waiting, started) if m)
    return {"systemMessage": msg} if msg else {}


def _stop_review(sid, cwd):
    """Thorough mode: starts a background review of the turn's changes; returns a line for the user or None."""
    cfg = config.load()
    if cfg["mode"] != "thorough":
        return None
    # shortcut: only the session's cwd repo is checked; work in other repos is caught at commit time
    root = packet.toplevel(cwd)
    if not root or packet.is_private(root, cfg) or _has_fix_pass(sid, "code_fix_pass", root):
        return None
    data = state.session(sid)
    # shortcut: files changed through Bash are not seen here; they are reviewed when committed
    pending, touched = data.get("code", {}).get(root, {}).get("pending", []), data.get("touched", {}).get(root, [])
    if not pending and not touched:
        return None
    diff = packet.change_diff(root, pending, [(root, ["-A", "--", *touched])] if touched else None, touched)
    digest = _digest(diff)
    if packet.is_small(diff) or data.get("stop_digest") == digest or _reused(state.repo(root), diff):
        return None
    data["stop_digest"] = digest
    if pending:  # in this review now; a failed one gives them back (_unreviewed)
        data["code"][root]["pending"] = []
    state.save_session(sid, data)
    prompt, pkt = prompts.code_review(packet.user_words(sid), diff, packet.decisions(root, cfg))
    _start_review(sid, "stop", root, prompt, pkt, cfg, pending)
    return "Argon is reviewing this turn's changes in the background"


WAKE_GRACE = 30  # seconds a waiter gives the commit's PostToolUse to start the review, and a finished one to land


def wake(inp, grace=WAKE_GRACE, poll=2):
    """The asyncRewake hook after a Bash call: when it was a commit, waits for the session's background reviews and
    returns their results, so Claude hears of them at once even when the session is idle. None: nothing to say
    (another hook took the result first, or no review ran)."""
    command = (inp.get("tool_input") or {}).get("command", "")
    if _sub(inp) or config.load()["mode"] == "off" or not packet.is_commit(command):
        return None
    sid = inp["session_id"]
    lock = os.path.join(state.dir(), "wake", sid)  # one waiter per session; it also waits for later commits' reviews
    with contextlib.suppress(OSError, ValueError):
        with open(lock) as f:
            if compat.pid_alive(int(f.read().strip())):
                return None
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "w") as f:
        f.write(str(os.getpid()))
    try:
        until = time.time() + grace
        while True:
            reap()  # a dead review lands in the inbox as such
            if any(m.get("sid") == sid and m.get("kind") in ("code", "stop") for m in state.running_jobs()):
                until = time.time() + grace  # its result lands just after the marker goes
            elif state.inbox_waiting().get(sid) or time.time() >= until:
                break
            time.sleep(poll)
        return _inbox(sid)[0] or None
    finally:
        with contextlib.suppress(OSError):
            os.remove(lock)
