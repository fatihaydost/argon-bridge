"""Session, repo and log state in $XDG_STATE_HOME/argon-bridge/."""
import contextlib
import hashlib
import itertools
import json
import os
import secrets
import time

SESSION_DAYS = 14  # session files keep the user's messages; older ones are deleted


def dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = os.path.join(base, "argon-bridge")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _read(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"  # two writers of one file never share a temp file
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:  # holds the user's messages
        json.dump(data, f)
    os.replace(tmp, p)


def _session_path(session_id):
    return os.path.join(dir(), "sessions", f"{session_id}.json")


def session(session_id):
    return _read(_session_path(session_id), {"prompts": []})


def save_session(session_id, data):
    _write(_session_path(session_id), data)


def _prune_sessions():
    """Session files and inbox results (a review can finish after its session ended) older than SESSION_DAYS."""
    cutoff = time.time() - SESSION_DAYS * 86400
    # only these two places: the state dir also holds worktrees, and on Windows links into the user's folder
    inbox = os.path.join(dir(), "inbox")
    tops = [os.path.join(dir(), "sessions")] + [os.path.join(inbox, n) for n in _names(inbox)]
    for top in tops:
        try:
            idle = os.path.getmtime(top) < cutoff  # read first: deleting files below touches the dir
        except OSError:
            continue
        for name in _names(top):
            p = os.path.join(top, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
        try:  # an old, empty inbox only, so a worker writing into a live one never loses its dir
            if top != tops[0] and not os.listdir(top) and idle:
                os.rmdir(top)
        except OSError:
            pass


def _names(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def append_prompt(session_id, text):
    data = session(session_id)
    if "prompt_count" not in data:  # a new session: a good moment to drop old ones
        _prune_sessions()
    data["prompt_count"] = data.get("prompt_count", 0) + 1  # only grows; fix-pass expiry counts on it
    prompts = data.setdefault("prompts", [])
    prompts.append(text)
    dropped = max(0, len(prompts) - 50)
    data["prompts"] = prompts[dropped:]
    data["review_mark"] = max(0, data.get("review_mark", 0) - dropped)
    save_session(session_id, data)


def current_session_id():
    """The Claude session that called us (Claude Code sets it in Bash), or None from the user's own terminal:
    never a guess, which with parallel sessions picks another conversation."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def _repo_path(root):
    return os.path.join(dir(), "repos", hashlib.sha1(root.encode()).hexdigest()[:16] + ".json")


def repo(root):
    return _read(_repo_path(root), {})


def save_repo(root, data):
    _write(_repo_path(root), data)


@contextlib.contextmanager
def owners(root):
    """The repository's shared commit record, read and written under one lock: `ledger` (commit sha -> the
    session that made it) and `inflight` (session -> where HEAD was when its commit command started)."""
    from .compat import locked
    p = _repo_path(root)[:-5] + "-owners.json"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with locked(p + ".lock"):
        data = _read(p, {})
        data.setdefault("ledger", {})
        data.setdefault("inflight", {})
        yield data
        _write(p, data)


def _stamp():
    """A file name that sorts by time and never repeats: Windows' clock ticks in ~16 ms, so two results written
    in one tick had one name and the second replaced the first; the counter keeps one process's writes in order."""
    return f"{time.time_ns()}-{next(_seq):09d}-{os.getpid()}-{secrets.token_hex(3)}"


_seq = itertools.count()


def new_job(job):
    path = os.path.join(dir(), "jobs", f"{_stamp()}.json")
    _write(path, job)
    return path


def _running_path(job_id):
    return os.path.join(dir(), "running", f"{job_id}.json")


def mark_running(job_id, info, pid=None):
    """`pid`: the worker's process, when the process that started it writes the marker (default: this one)."""
    _write(_running_path(job_id), {**info, "pid": pid or os.getpid(), "started": time.time()})


def update_running(job_id, fields):
    """Adds `fields` to a marker this process holds (a phase's latest agy event); a cleared marker stays gone."""
    m = _read(_running_path(job_id), None)
    if isinstance(m, dict):
        _write(_running_path(job_id), {**m, **fields})


def clear_running(job_id):
    """The marker, and the job's spec in case its worker was stopped before it read it (the user's words)."""
    for p in (_running_path(job_id), os.path.join(dir(), "jobs", f"{job_id}.json")):
        try:
            os.remove(p)
        except OSError:
            pass


def running_jobs():
    d = os.path.join(dir(), "running")
    jobs = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if name.endswith(".json"):
            m = _read(os.path.join(d, name), None)
            if isinstance(m, dict):
                jobs.append({**m, "id": name[:-5]})
    return jobs


def _alive(pid):
    from .compat import pid_alive  # never os.kill: on Windows it terminates the process
    return pid_alive(pid)


def reap_dead(now=None):
    """Removes and returns the markers of background jobs whose process is gone or far past its time limit."""
    now = time.time() if now is None else now
    dead = []
    for m in running_jobs():
        if _alive(m.get("pid")) and now - m.get("started", now) <= 2 * m.get("timeout", 900) + 120:
            continue
        try:
            os.remove(_running_path(m["id"]))  # whoever removes it reports it, so it is reported once
        except OSError:
            continue
        try:  # a worker that died before it read its spec leaves the spec (the user's words) behind
            os.remove(os.path.join(dir(), "jobs", m["id"] + ".json"))
        except OSError:
            pass
        dead.append(m)
    return dead


def inbox_put(session_id, item):
    """A background result waiting to be handed to the session's Claude."""
    _write(os.path.join(dir(), "inbox", session_id, f"{_stamp()}.json"), item)


def inbox_waiting():
    """{session id: number of results waiting for it}."""
    inbox = os.path.join(dir(), "inbox")
    counts = {sid: sum(n.endswith(".json") for n in _names(os.path.join(inbox, sid))) for sid in _names(inbox)}
    return {sid: n for sid, n in counts.items() if n}


def inbox_take(session_id):
    """Takes every waiting result once, oldest first; a concurrent hook cannot take the same one."""
    d = os.path.join(dir(), "inbox", session_id)
    items = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            os.rename(p, p + ".taken")
        except OSError:
            continue
        items.append(_read(p + ".taken", None))
        os.remove(p + ".taken")
    return [i for i in items if i]


def log(event):
    with open(os.open(os.path.join(dir(), "log.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}) + "\n")


def recent_log(n=20):
    try:
        with open(os.path.join(dir(), "log.jsonl")) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


FINDINGS_MAX = 2 * 1024 * 1024
FINDING_KEYS = ("file", "line", "claim", "severity", "kind", "unverified", "origin")


def log_findings(review, root, head, findings):
    """Every finding Argon reported, kept beyond the chat: a weekly skim catches a finding wrongly rejected.
    root: the user's repository (never a snapshot), head: the reviewed commit."""
    if not findings:
        return
    p = os.path.join(dir(), "findings.jsonl")
    with contextlib.suppress(OSError):
        if os.path.getsize(p) > FINDINGS_MAX:
            # shortcut: a writer appending meanwhile can lose its lines; a lock if the log ever matters more
            with open(p) as f:
                lines = f.readlines()
            tmp = f"{p}.{os.getpid()}.tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.writelines(lines[len(lines) // 2:])
            os.replace(tmp, p)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        f.writelines(json.dumps({"ts": ts, "review": review, "root": root, "head": head,
                                 **{k: x[k] for k in FINDING_KEYS if k in x},
                                 "evidence": str(x.get("evidence", ""))[:300]}) + "\n" for x in findings)


def recent_findings(days=7):
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
    try:
        with open(os.path.join(dir(), "findings.jsonl")) as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        with contextlib.suppress(ValueError):
            e = json.loads(line)
            if isinstance(e, dict) and e.get("ts", "") >= since:
                out.append(e)
    return out


def cooldown_until():
    return _read(os.path.join(dir(), "cooldown.json"), {}).get("until", 0)


def set_cooldown(seconds):
    _write(os.path.join(dir(), "cooldown.json"), {"until": time.time() + seconds})
