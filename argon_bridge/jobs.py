"""Delegated jobs: Argon works on branch argon/<id> in a worktree under state.dir(), never in the user's folder.
A job is a plain dict saved as state.dir()/jobs-state/<id>.json (see `create` for its keys)."""
import contextlib
import os
import re
import subprocess
import time

from . import compat, config, packet, state


def _dir():
    return os.path.join(state.dir(), "jobs-state")


def _path(job_id):
    return os.path.join(_dir(), f"{job_id}.json")


def _git(root, *args):
    p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout.strip()


def load(job_id):
    job = state._read(_path(job_id), None)
    if not isinstance(job, dict):
        raise ValueError(f"no delegated job {job_id!r}")
    return job


def save(job):
    job["updated"] = time.time()
    state._write(_path(job["id"]), job)


def all():
    d = _dir()
    found = [state._read(os.path.join(d, n), None) for n in sorted(os.listdir(d)) if n.endswith(".json")] \
        if os.path.isdir(d) else []
    return sorted((j for j in found if isinstance(j, dict)), key=lambda j: j.get("created", 0))


def create(sid, repo, name, phases, context, skills):
    root = packet.toplevel(repo)
    if not root:
        raise ValueError(f"{repo} is not inside a git repository")
    if packet.is_private(root, config.load()):
        raise ValueError(f"{root} is in private_roots; Argon may not work there")
    if not phases or any(not isinstance(p, dict) or not {"title", "goal", "check"} <= set(p)
                         or not {"command", "expect"} <= set(p["check"] or {}) for p in phases):
        raise ValueError("every phase needs title, goal and check {command, expect}")
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "job"
    job_id = f"{slug}-{time.strftime('%Y%m%d%H%M')}"
    n = 1
    while os.path.exists(_path(job_id)) or _git(root, "branch", "--list", f"argon/{job_id}"):
        n += 1
        job_id = f"{slug}-{time.strftime('%Y%m%d%H%M')}-{n}"
    worktree = os.path.join(state.dir(), "worktrees", job_id)
    base = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if base == "HEAD":
        raise ValueError(f"{root} is on a detached HEAD; check out the branch the work should go to first")
    _git(root, "branch", f"argon/{job_id}", "HEAD")
    os.makedirs(os.path.dirname(worktree), exist_ok=True)
    _git(root, "worktree", "add", "-q", worktree, f"argon/{job_id}")
    now = time.time()
    job = {
        "id": job_id, "name": name, "sid": sid, "repo": root, "base": base, "branch": f"argon/{job_id}",
        "worktree": worktree, "current": 0, "conversation": None, "context": list(context),
        "skills": list(skills), "allow_extra": [], "state": "running", "created": now, "updated": now, "tokens": 0,
        "phases": [{"title": p["title"], "goal": p["goal"], "check": p["check"], "status": "pending", "rounds": 0,
                    "report": None, "commit": None} for p in phases],
    }
    save(job)
    return job


def remove_worktree(job):
    """Deletes the job's worktree and branch (after merge or reject) and the exclude lines `prepare` added."""
    if job.get("excluded"):
        try:
            p = _exclude(job)
            with open(p) as f:
                lines = f.read().splitlines()
            for line in set(job["excluded"]) - _others_lines(job):  # another open job may still need it
                if line in lines:
                    lines.remove(line)
            with open(p, "w") as f:
                f.write("".join(f"{line}\n" for line in lines))
        except (OSError, RuntimeError):
            pass
    for rel in job.get("linked", []):  # unlink first: deleting the worktree must never walk into the user's folder
        with contextlib.suppress(OSError):
            compat.unlink(os.path.join(job["worktree"], rel))
    for args in (("worktree", "remove", "--force", job["worktree"]), ("worktree", "prune"),
                 ("branch", "-D", job["branch"])):
        try:
            _git(job["repo"], *args)
        except RuntimeError:
            pass


def prepare(job, links):
    """Symlinks each relative path in `links` (dependencies, `.env`) from the user's folder into the worktree,
    when it exists there and not yet in the worktree, and keeps it out of git; returns the paths linked.
    info/exclude is shared by all worktrees of the repo, so the lines added here are removed with the worktree."""
    linked = []
    for rel in links:
        rel = os.path.normpath(rel)
        src = os.path.realpath(os.path.join(job["repo"], rel))
        dst = os.path.join(job["worktree"], rel)
        if os.path.isabs(rel) or rel == "." or rel.startswith(".." + os.sep) or rel == ".." \
                or not src.startswith(job["repo"] + os.sep) or not os.path.exists(src) or os.path.lexists(dst) \
                or rel in linked:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        compat.link(src, dst)
        linked.append(rel)
        # recorded at once: if a later link fails, closing the job must still unlink this one first
        job.setdefault("linked", []).append(rel)
        exclude(job, [rel])
    return linked


def exclude(job, rels):
    """Keeps worktree paths out of git through info/exclude (shared by all worktrees of the repo, so the lines
    added here are recorded in the job and removed with the worktree); saves the job."""
    path = _exclude(job)
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        text = ""
    present, others = set(text.splitlines()), _others_lines(job)
    for rel in rels:
        line = "/" + rel.replace(os.sep, "/")  # git patterns use / on every platform
        if line in job.get("excluded", []):
            continue
        if line not in present:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(("\n" if text and not text.endswith("\n") else "") + line + "\n")
            text, present = text + line + "\n", present | {line}
        elif line not in others:
            continue  # the user's own line: never removed
        job.setdefault("excluded", []).append(line)
    save(job)


def _others_lines(job):
    """Exclude lines held by the other open jobs on the same repository."""
    return {line for j in all() if j["id"] != job["id"] and j.get("repo") == job["repo"]
            and j.get("state") not in ("merged", "rejected") for line in j.get("excluded", [])}


def _exclude(job):
    p = _git(job["worktree"], "rev-parse", "--git-path", "info/exclude")
    return p if os.path.isabs(p) else os.path.join(job["worktree"], p)
