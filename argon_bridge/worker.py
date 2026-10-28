"""Delegated work: Argon carries out a phased plan in the job's worktree, Claude checks every phase.
Claude drives it with `argon-bridge delegate …`; each phase runs as a background worker (hooks.start_background)
and its result reaches the owning session through the inbox and `delegate wait`."""
import json
import os
import re
import subprocess
import sys
import time

from .cli import _opt, _opts, _unknown
from . import compat, config, context, framing, gateway, hooks, jobs, permissions, prompts, skills, state

MAX_ROUNDS = 2
CLOSED = ("merged", "rejected")
# exact forms, not prefixes: agy anchors a regex rule, and a prefix rule would also pass `git diff '/abs'`,
# `"$HOME/…"`, `ls $(cat …)` or `git status; …`. Words hold no quotes, $, `, ;, |, &, <, >, spaces or newlines
# ([ \t], not \s), so no shell is reached through them; git diff takes no path (one outside the worktree
# makes it read any file)
_WORD = r"[\w.~^@:/+=,%-]+"
WORKER_ALLOW = [r"command(regex:ls([ \t]+[\w./+=,-]+)*)",
                r"command(regex:wc([ \t]+-[lwcmL]+)*([ \t]+[\w./][\w./+,-]*)*)",  # no long options: --files0-from
                r"command(regex:git status([ \t]+[\w=-]+)*)",
                r"command(regex:git diff([ \t]+(HEAD(~\d+)?|--stat|--cached|--staged|--name-only|--name-status|"
                r"--shortstat))*)",
                rf"command(regex:git log([ \t]+{_WORD})*)",
                rf"command(regex:git show([ \t]+{_WORD})*)"]
# no shell reading commands (cat, grep, …): an allowed one bypasses read_file deny rules (agy-probes.md)
# [\s\S], not `.`: a command may span lines
WORKER_DENY = [r"command(regex:[\s\S]*(\.env|\.pem|\.key|\.p12|\.pfx|\.keystore|\.jks|id_rsa|id_ed25519|id_ecdsa|"
               r"credentials|\.ssh|\.aws|gcloud|\.config/gh|\.netrc|\.docker|\.kube|\.gnupg|\.npmrc|\.pypirc)[\s\S]*)",
               # git diff reads any file given a path outside the worktree (it implies --no-index), wc a file list
               r"command(regex:git\s+diff[\s\S]*[\s=](/|~|[A-Za-z]:[\\/])[\s\S]*)",
               r"command(regex:[\s\S]*(^|[\s=/\\])\.\.[\\/][\s\S]*)",
               r"command(regex:[\s\S]*(--no-index|--files0-from)[\s\S]*)",
               r"command(regex:git\s+(log|show)[\s\S]*\s--ou[\s\S]*)",  # --output=<file> writes anywhere
               "command(git push)", "command(git remote)", r"command(regex:[\s\S]*\b(deploy|publish)\b[\s\S]*)",
               "command(curl)", "command(wget)", "command(ssh)", "command(scp)", "command(rsync)", "command(sudo)",
               "command(rm -rf)", "command(npm publish)", "command(gh)", "command(docker push)"]
USAGE = """usage: argon-bridge delegate <command>   (only when the user asked to hand work to Argon)
  start --repo R --name N --plan FILE [--context P ...] [--skills a,b] [--link PATH ...] [--allow PREFIX ...]
        [--setup CMD ...]      shell commands run in order in the worktree before phase 1
  wait JOB [--timeout S]       block until the running phase ends; prints its report and your next step
  check JOB pass --message MSG | fail --notes TEXT [--note-for-argon LINE]
  answer JOB TEXT | allow JOB PREFIX | resume JOB
  merge JOB [--force-dirty] | reject JOB | adopt JOB | status"""


def rules_for(job):
    """agy permission rules for a phase run: the worker allow-list plus the plan's check commands and the
    prefixes Claude allowed for this job; secrets (also behind links into the user's folder) and outside-world
    commands denied."""
    allow = list(WORKER_ALLOW)
    for c in [r for p in job["phases"] for r in check_rules(p["check"]["command"])] + \
            [f"command({p})" for p in job["allow_extra"]]:
        if c not in allow:
            allow.append(c)
    deny = []
    # the worktree's files, the user's copies behind links, and the user's folder itself (unlinked secrets too)
    repo = permissions.secret_rules(job["repo"])
    found = permissions.secret_rules(job["worktree"]) + repo
    found += [f"read_file({os.path.realpath(r[10:-1])})" for r in found if os.path.islink(r[10:-1])]
    # the same files seen through a --link'ed directory of the worktree (os.walk does not follow links)
    found += [f"read_file({job['worktree']}{r[10 + len(job['repo']):-1]})" for r in repo
              if r[10:-1].startswith(job["repo"] + os.sep)]
    for r in found + WORKER_DENY:
        if r not in deny:
            deny.append(r)
    return {"allow": allow, "deny": deny}


_UNSAFE = re.compile(r"[\"'$`;|&<>\\\n\r]")


def check_rules(command):
    """A plan's check as exact-form rules: each part of an `a && b && c` chain and the whole chain (agy ran a
    chain whose parts were allowed one by one; the whole-chain rule passed in one probe and not in another,
    agy-probes.md). A part holding quotes, $, `, ;, |, &, redirections or a backslash gets no rule, nor does the
    chain: the lead runs that check itself."""
    parts = [p.strip() for p in command.split("&&")]
    safe = [p for p in parts if p and not _UNSAFE.search(p)]
    rules = [_exact(p) for p in safe]
    if len(parts) > 1 and len(safe) == len(parts):
        rules.append(_exact(command.strip()))
    return rules


def _exact(command):
    return "command(regex:" + re.escape(command).replace("\\ ", " ") + ")"  # a plain space reads the same


def _phase_text(job, i):
    p = job["phases"][i]
    rep = p.get("report") or {}
    lines = []
    if rep.get("summary"):
        lines.append(f"summary: {rep['summary']}")
    for key in ("changes", "commands_run", "failures"):
        if rep.get(key):
            lines.append(f"{key}: " + "; ".join(rep[key]))
    for key in ("question", "subagents"):
        if rep.get(key):
            lines.append(f"{key}: {rep[key]}")
    if rep.get("denied"):
        lines.append("denied: " + "; ".join(rep["denied"]))
    # Argon's words reach Claude through a hook, a trusted channel: fenced as data, like review findings
    return (f"Argon finished phase {i + 1} of {job['id']} ({p['title']}): {p['status']}\n"
            + framing.untrusted("ARGON PHASE REPORT", "Argon's claims, verify them; merge only after the user "
                                "approves", "\n".join(lines)) + "\nNext: " + next_step(job))


def next_step(job):
    jid, wt = job["id"], job["worktree"]
    p = job["phases"][job["current"]] if job["current"] < len(job["phases"]) else None
    st = job["state"]
    if st == "waiting_merge":
        return (f"all phases passed. Summarize the work for the user and ask for approval; only then "
                f"`argon-bridge delegate merge {jid}` (or `reject`).")
    if st in ("merged", "rejected"):
        return "nothing; the job is closed."
    if st == "paused_quota":
        return f"Argon's quota ran out. Tell the user; do not take the work over. Later: `argon-bridge delegate resume {jid}`."
    if st == "dead":
        return f"the phase worker died. Tell the user; `argon-bridge delegate resume {jid}` runs the phase again."
    if st == "running":
        return f"wait: `argon-bridge delegate wait {jid}` (run it in the background)."
    if p["status"] == "question":
        return (f"answer with `argon-bridge delegate answer {jid} \"…\"` when the plan, rules or code settle it; "
                "otherwise ask the user.")
    if p["status"] == "denied":
        return (f"if the denied command is local and safe: `argon-bridge delegate allow {jid} <prefix>`; otherwise, "
                f"and for a denied read_file (outside what Argon may read), `argon-bridge delegate answer {jid} "
                f"\"do it another way: …\"`. Tell the user either way.")
    if p["status"] == "stalled":
        return (f"Argon went silent and was stopped; tell the user. See what it left: `git -C {wt} status`. Then "
                f"`argon-bridge delegate resume {jid}` (continues the phase in the same conversation), or if the "
                f"work is complete check it: `argon-bridge delegate check {jid} pass --message \"…\"` or `fail "
                f"--notes \"…\"`; `argon-bridge delegate reject {jid}` drops the job.")
    if p["status"] == "claude_takeover":
        return (f"the fix rounds are used up: fix the phase yourself in {wt}, tell the user, then "
                f"`argon-bridge delegate check {jid} pass --message \"…\"`.")
    return (f"check it: run `{p['check']['command']}` in {wt} (expect: {p['check']['expect']}), read "
            f"`git -C {wt} add -AN && git -C {wt} diff HEAD`, compare with the plan and rules, look for anything "
            f"touching the outside world or secrets. Then `argon-bridge delegate check {jid} pass --message \"…\"` "
            f"or `argon-bridge delegate check {jid} fail --notes \"…\"`.")


def _start(job, notes=None, answer=None):
    """Runs the current phase (again) in the background, in the same agy conversation."""
    p = job["phases"][job["current"]]
    p["status"] = "running"
    p["notes"], p["answer"] = notes, answer
    job["state"] = "running"
    jobs.save(job)
    cfg = config.load()
    try:
        hooks.start_background({"type": "phase", "job_id": job["id"], "sid": job["sid"], "root": job["repo"],
                                "timeout": cfg["worker_timeout"]}, job["worktree"])
    except Exception as e:
        _start_failed(job, e)
        raise


def _start_failed(job, e):
    """A phase that never got a worker: `dead`, so `resume` and `reject` work on it (not `running` forever)."""
    p = job["phases"][job["current"]]
    p["status"], job["state"] = "failed", "dead"
    p["report"] = {"summary": f"the phase did not start: {type(e).__name__}: {e}"}
    jobs.save(job)


def run_phase(job_id):
    """The background body of one phase run."""
    job = jobs.load(job_id)
    i = job["current"]
    p = job["phases"][i]
    cfg = config.load()
    try:
        text, skipped = context.build(job["context"], job["repo"])
        missing = [s for s in skipped if not os.access(s, os.R_OK)]
        if missing:
            text += "\n# Context files that could not be read\n\n" + "".join(f"- {m}\n" for m in missing)
        prompt, pkt = prompts.worker(job, p, text, p.get("notes"), p.get("answer"))
        jobs.exclude(job, skills.deliver(job["skills"], job["worktree"]))
        result = gateway.work(job, p, prompt, pkt, cfg["worker_effort"], cfg["worker_timeout"], rules_for(job), [],
                              stall=cfg["stall_seconds"], on_event=_marker_note(job_id))
    except Exception as e:  # the session must still hear what happened
        result = gateway.Result(False, error="error", detail=f"{type(e).__name__}: {e}")
    job = jobs.load(job_id)  # Claude may have adopted the job meanwhile
    if job["state"] in CLOSED:  # rejected while this phase ran
        return
    p = job["phases"][i]
    if result.error != "quota":  # a paused run may not have reached Argon: `resume` sends them again
        p.pop("notes", None)
        p.pop("answer", None)
    job["tokens"] = job.get("tokens", 0) + int((result.usage or {}).get("total_tokens") or 0)
    if not result.ok and (result.data or {}).get("conversation_id"):  # the turn reached Argon: later turns go on in it
        job["conversation"] = result.data["conversation_id"]
    if result.ok:
        rep = result.data
        if rep.get("conversation_id"):
            job["conversation"] = rep["conversation_id"]
        p["report"] = {k: v for k, v in rep.items() if k != "conversation_id"}
        p["status"] = {"done": "done", "question": "question", "denied": "denied"}.get(rep["status"], "failed")
        job["state"] = "waiting_answer" if p["status"] == "question" else "waiting_check"
    elif result.error == "quota":
        p["status"] = job["state"] = "paused_quota"
        p["report"] = {"summary": f"quota: {result.detail}"}
    elif result.error == "stalled":  # the work so far is in the worktree, the conversation can go on
        p["status"], job["state"] = "stalled", "waiting_check"
        p["report"] = {"summary": f"stalled: {result.detail}"}
    elif result.error == "malformed":  # the turn ended, only its report is broken: the work is still checked
        p["status"], job["state"] = "report_malformed", "waiting_check"
        raw = f"; raw output: {result.raw}" if result.raw else ""
        p["report"] = {"summary": f"report_malformed: {result.detail}{raw}"}
    else:
        p["status"], job["state"] = "failed", "waiting_check"
        p["report"] = {"summary": f"{result.error}: {result.detail}"}
    p["duration"] = p.get("duration", 0) + result.duration
    # the note first: `wait` takes this job's notes as soon as it sees the new state
    state.inbox_put(job["sid"], {"kind": "phase", "job": job_id, "root": job["repo"], "status": _phase_text(job, i),
                                 "findings": None})
    jobs.save(job)


def _marker_note(job_id):
    """Writes agy's pid and latest event into the running marker of this job's phase (`delegate status`)."""
    def note(fields):
        for m in state.running_jobs():
            if m.get("job_id") == job_id:
                try:
                    state.update_running(m["id"], fields)
                except OSError:  # a status aid must not fail the phase
                    pass
    return note


def mark_dead(m):
    """A phase worker reaped by state.reap_dead: the phase failed, the job is dead, the owner is told."""
    compat.stop_agy(m.get("agy_pid"))  # agy runs in a session of its own: it outlives its worker
    try:
        job = jobs.load(m["job_id"])
    except (ValueError, KeyError):
        return
    if job["state"] in CLOSED:
        return
    p = job["phases"][job["current"]]
    if p["status"] == "running":
        p["status"], job["state"] = "failed", "dead"
        p["report"] = {"summary": "worker died (process gone or far past its time limit)"}
        jobs.save(job)
    state.inbox_put(job["sid"], {"kind": "phase", "job": job["id"], "root": job["repo"], "findings": None,
                                 "status": f"⚠️ Argon's phase worker for {job['id']} died.\nNext: " + next_step(job)})


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def _move_inbox(old, new, job_id):
    """The notes about this job still waiting for its old owner go to the new one; new=None drops them
    (`wait` printed the result, the same note need not arrive again through a hook)."""
    d = os.path.join(state.dir(), "inbox", old)
    for name in sorted(os.listdir(d)) if old != new and os.path.isdir(d) else []:
        p = os.path.join(d, name)
        item = state._read(p, None)
        if isinstance(item, dict) and item.get("job") == job_id:
            if new:
                state.inbox_put(new, item)
            try:
                os.remove(p)
            except OSError:
                pass


def _wait(job_id, timeout):
    end = time.time() + timeout
    while True:
        hooks.reap()  # a dead worker would leave the job running until a hook fires
        job = jobs.load(job_id)
        if job["state"] != "running" or time.time() > end:
            break
        time.sleep(0 if os.environ.get("ARGON_REVIEWER_FOREGROUND") else 5)
    if job["state"] == "running":
        print(f"still running after {timeout}s. Next: " + next_step(job))
        return 1
    _move_inbox(job["sid"], None, job_id)
    i = min(job["current"], len(job["phases"]) - 1)
    print(_phase_text(job, i) if job["state"] != "waiting_merge" else "Next: " + next_step(job))
    return 0


def _commit(job, message):
    _git(job["worktree"], "add", "-A")
    if _git(job["worktree"], "diff", "--cached", "--quiet").returncode == 0:
        return None  # nothing changed in this phase
    c = _git(job["worktree"], "commit", "-q", "-m", message)
    if c.returncode != 0:
        raise RuntimeError(f"commit failed: {(c.stderr or c.stdout).strip()[:300]}")
    return _git(job["worktree"], "rev-parse", "HEAD").stdout.strip()


def _check(job, verdict, argv):
    if job["state"] not in ("waiting_check", "waiting_answer", "dead"):  # first: a finished job has no phase left
        raise ValueError(f"job is {job['state']}; nothing to check")
    p = job["phases"][job["current"]]
    if verdict == "pass":
        msg = _opt(argv, "--message")
        _unknown(argv, words=False)
        if not msg:
            raise ValueError("check pass needs --message (the commit message, no AI mention)")
        p["commit"] = _commit(job, msg)
        p["status"] = "done"
        job["current"] += 1
        if job["current"] >= len(job["phases"]):
            job["state"] = "waiting_merge"
            jobs.save(job)
        else:
            _start(job)  # the next phase continues the same agy conversation
        return f"phase {job['current']} committed. Next: " + next_step(job)
    if verdict != "fail":
        raise ValueError("check JOB pass|fail")
    notes = _opt(argv, "--notes")
    if not notes:
        raise ValueError("check fail needs --notes")
    line = _opt(argv, "--note-for-argon")
    _unknown(argv, words=False)
    if line:
        context.add_note(job["repo"], line)
    p["rounds"] += 1
    if p["rounds"] > MAX_ROUNDS:
        p["status"], job["state"] = "claude_takeover", "waiting_check"
        jobs.save(job)
        return "fix rounds used up. Next: " + next_step(job)
    _start(job, notes=notes)
    return f"fix round {p['rounds']} of {MAX_ROUNDS} started. Next: " + next_step(job)


def _claim(repo, shas):
    """Every phase was checked: no session's commit window claims the job's commits for its review."""
    with state.owners(repo) as o:
        o["ledger"].update({s: {"sid": "delegate", "t": time.time()} for s in shas})


def _merge(job, argv):
    if job["state"] != "waiting_merge":
        raise ValueError(f"job is {job['state']}; only a job whose phases all passed can be merged")
    branch = _git(job["repo"], "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != job["base"]:
        raise ValueError(f"{job['repo']} is on {branch}, the job started from {job['base']}; "
                         "switch back (or ask the user) first")
    if _git(job["repo"], "status", "--porcelain").stdout.strip() and "--force-dirty" not in argv:
        raise ValueError(f"{job['repo']} has uncommitted changes; ask the user before --force-dirty")
    before = _git(job["repo"], "rev-parse", "HEAD").stdout.strip()
    m = _git(job["repo"], "merge", "--ff-only", job["branch"])  # flat history: no empty merge commit
    if m.returncode != 0:
        if _git(job["repo"], "merge-base", "--is-ancestor", before, job["branch"]).returncode != 0:
            raise ValueError(f"{job['base']} moved since the job started, so it cannot fast-forward; nothing "
                             f"changed. Ask the user: `git -C {job['worktree']} rebase {job['base']}` (the branch is "
                             "checked out there), then merge again")
        raise ValueError(f"merge failed, nothing changed: {(m.stdout + m.stderr).strip()[:400]}")
    _claim(job["repo"], _git(job["repo"], "rev-list", f"{before}..HEAD").stdout.split())
    _close(job, "merged")
    return f"merged {job['branch']} into {branch}; worktree and branch removed"


def _stop_worker(job):
    """Stops the phase worker of a running job, so closing it does not pull its worktree from under agy."""
    hooks.reap()  # a dead or stale marker is not a process to stop
    for m in state.running_jobs():
        if m.get("job_id") == job["id"]:
            compat.stop_agy(m.get("agy_pid"))  # agy runs in a session of its own (gateway._stream)
        if m.get("job_id") == job["id"] and compat.pid_alive(m.get("pid")):
            compat.kill_tree(m["pid"])
        if m.get("job_id") == job["id"]:
            state.clear_running(m["id"])


def _close(job, final):
    was_running = job["state"] == "running"
    job["state"] = final
    jobs.save(job)  # closed first: a worker reaped or finishing meanwhile then leaves no note (CLOSED)
    if was_running:
        _stop_worker(job)
        jobs.save(job)  # again: the worker may have saved its own `running` copy before it was stopped
    jobs.remove_worktree(job)
    try:  # a run killed mid-phase leaves its job project file (permissions.hold name)
        os.remove(os.path.join(permissions.projects_dir(), f"argon-bridge-job-{job['id']}.json"))
    except OSError:
        pass
    _move_inbox(job["sid"], None, job["id"])  # notes about a closed job are stale


def _calling_session():
    """A job's results go to the Claude session that runs `delegate`; from a plain terminal there is none."""
    sid = state.current_session_id()
    if not sid:
        raise ValueError("run this from Claude Code: CLAUDE_CODE_SESSION_ID is not set, so there is no session "
                         "to send the job's results to")
    return sid


def _start_job(argv):
    sid = _calling_session()
    repo, name, plan_file = _opt(argv, "--repo"), _opt(argv, "--name"), _opt(argv, "--plan")
    if not (repo and name and plan_file):
        raise ValueError("start needs --repo, --name and --plan")
    with open(plan_file) as f:
        plan = json.load(f)
    if not isinstance(plan, dict):
        raise ValueError("the plan file must be a JSON object {goal, phases}")
    given, refused = skills.allowed([s for s in (_opt(argv, "--skills") or "").split(",") if s.strip()], repo)
    for n, why in refused:
        print(f"skill {n} not given: {why}", file=sys.stderr)
    ctx = [os.path.abspath(os.path.expanduser(p)) for p in _opts(argv, "--context")]
    links, allows, setups = _opts(argv, "--link"), _opts(argv, "--allow"), _opts(argv, "--setup")
    _unknown(argv, words=False)  # before the job and its worktree exist
    job = jobs.create(sid, repo, name, plan.get("phases") or [], ctx, given)
    job["goal"] = str(plan.get("goal") or "")
    job["allow_extra"] = allows
    jobs.save(job)
    print(job["id"])  # first: a start that fails below still names the job to `reject`
    try:
        linked = jobs.prepare(job, links)
    except Exception as e:
        _start_failed(job, e)  # the job in memory: it holds every link made, saved or not
        raise
    if linked:
        print(f"linked into the worktree: {', '.join(linked)}", file=sys.stderr)
    job = jobs.load(job["id"])
    try:
        for cmd in setups:  # before phase 1, not beside it (an install racing the phase, 23.10)
            _setup(job, cmd)
    except Exception as e:
        _start_failed(job, e)
        raise
    _start(job)
    return None


def _setup(job, cmd):
    """A lead-given setup command (an install, a code generator) run to its end in the worktree, through the
    shell; its output's tail goes to stderr."""
    r = subprocess.run(cmd, shell=True, cwd=job["worktree"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       errors="replace")
    tail = (r.stdout + r.stderr).strip()[-2000:]
    if tail:
        print(tail, file=sys.stderr)
    if r.returncode != 0:
        raise RuntimeError(f"setup `{cmd}` exited with {r.returncode}; no phase started")


def status_lines():
    hooks.reap()
    out = []
    for j in jobs.all():
        done = [p["commit"] for p in j["phases"] if p.get("commit")]  # the phase commits outlive a deleted branch
        if j["state"] == "waiting_merge" and done and _git(j["repo"], "merge-base", "--is-ancestor", done[-1],
                                                           j["base"]).returncode == 0:
            _claim(j["repo"], done)
            _close(j, "merged")  # merged by hand: nothing left to merge
            out.append(f"{j['id']} merged (external)")
            continue
        n = len(j["phases"])
        rounds = sum(p.get("rounds", 0) for p in j["phases"])
        takeover = "yes" if any(p["status"] == "claude_takeover" for p in j["phases"]) else "no"
        age = int(time.time() - j.get("created", time.time()))
        age = f"{age // 3600}h{age % 3600 // 60:02d}m" if age >= 3600 else f"{age // 60}m"
        live = ""
        if j["state"] == "running":
            m = next((m for m in state.running_jobs() if m.get("job_id") == j["id"]), {})
            live = (f", last model event {int(time.time() - m['last_event'])}s ago ({m.get('event')})"
                    if m.get("last_event") else ", no model event yet")
        elif j["current"] < n and j["phases"][j["current"]]["status"] == "stalled":
            live = ", stalled: " + (j["phases"][j["current"]].get("report") or {}).get("summary", "")[9:]
        out.append(f"{j['id']} {j['state']} phase {min(j['current'] + 1, n)}/{n}, fix rounds {rounds}, "
                   f"takeover {takeover}, argon tokens {j.get('tokens', 0)}{live}, {age}")
    return out or ["(none)"]


def main(argv):
    if not argv:
        print(USAGE)
        return 2
    cmd, argv = argv[0], list(argv[1:])
    try:
        if cmd == "start":
            return _start_job(argv) or 0
        if cmd == "status":
            print("\n".join(status_lines()))
            return 0
        if not argv:
            raise ValueError(USAGE)
        job = jobs.load(argv.pop(0))
        if cmd == "wait":
            t = _opt(argv, "--timeout")
            _unknown(argv, words=False)
            return _wait(job["id"], int(t) if t else 2 * config.load()["worker_timeout"] + 600)
        if cmd == "check":
            msg = _check(job, argv.pop(0) if argv else "", argv)
        elif cmd == "answer":
            if job["state"] not in ("waiting_answer", "waiting_check") or not argv:
                raise ValueError("answer JOB TEXT, for a phase that asked a question or was denied")
            _start(job, answer=" ".join(argv))
            msg = "answer sent; Argon continues in the same conversation. Next: " + next_step(job)
        elif cmd == "allow":
            if not argv:
                raise ValueError("allow JOB PREFIX")
            job["allow_extra"].append(" ".join(argv))
            if job["phases"][min(job["current"], len(job["phases"]) - 1)]["status"] == "denied":
                _start(job, answer=f"The command prefix `{' '.join(argv)}` is now allowed; continue the phase.")
            else:
                jobs.save(job)
            msg = "allowed. Next: " + next_step(jobs.load(job["id"]))
        elif cmd == "resume":
            stalled = job["state"] == "waiting_check" and job["phases"][job["current"]]["status"] == "stalled"
            if job["state"] not in ("paused_quota", "dead") and not stalled:
                raise ValueError(f"job is {job['state']}; only a paused, dead or stalled job resumes")
            state.set_cooldown(0)  # an explicit retry: the quota guess must not block it
            p = job["phases"][job["current"]]  # fix notes or an answer the stopped run may never have sent
            _start(job, notes=p.get("notes"), answer=p.get("answer") or "Continue the phase where you stopped.")
            msg = "resumed. Next: " + next_step(job)
        elif cmd == "merge":
            _unknown([a for a in argv if a != "--force-dirty"], words=False)
            msg = _merge(job, argv)
        elif cmd == "reject":
            _close(job, "rejected")
            msg = "rejected; worktree and branch removed"
        elif cmd == "adopt":
            old, job["sid"] = job["sid"], _calling_session()
            jobs.save(job)
            _move_inbox(old, job["sid"], job["id"])
            msg = f"this session now owns {job['id']}"
        else:
            raise ValueError(USAGE)
    except (ValueError, OSError, RuntimeError) as e:
        print(f"delegate {cmd}: {e}", file=sys.stderr)
        return 1
    print(msg)
    return 0
