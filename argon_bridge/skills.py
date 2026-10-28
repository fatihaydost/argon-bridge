"""Skills Claude picks for a delegated job: found among Claude's installed skills, filtered by the never-list,
and copied into the job's worktree under .agents/skills/, kept out of git (SKILL_DELIVERY = worktree_exclude, agy-probes.md:
a folder holding .agents/ passed with --add-dir becomes agy's command folder)."""
import json
import os
import re
import shutil

from . import config, packet

OWN_SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
_MEMORY = re.compile(r"^(.*memory.*|consolidate-memory|import-memory)$", re.I)
_OUTSIDE = re.compile(r"\b(deploy|publish|release|push|send|email|slack|linear|outline|drive|docs|sheets|slides|"
                      r"schedule|cron|morning|gmail|calendar|post)\b", re.I)


def _frontmatter(path):
    """(name, description) from a SKILL.md; description may be a folded (`>`/`|`) block."""
    try:
        with open(path) as f:
            text = f.read(8000)
    except OSError:
        return None, ""
    if not text.startswith("---"):
        return None, ""
    lines = text.split("\n---", 1)[0].splitlines()[1:]
    name, desc = None, ""
    for i, line in enumerate(lines):
        if line.startswith("name:"):
            name = line[5:].strip().strip("\"'") or None
        elif line.startswith("description:"):
            desc = line[12:].strip()
            more = []
            for ln in lines[i + 1:]:  # folded, literal, plain multi-line or wrapped: every indented line
                if not ln.startswith((" ", "\t")):
                    break
                more.append(ln.strip())
            desc = " ".join(([] if desc in ("", ">", "|", ">-", "|-") else [desc]) + more)
    return name, desc.strip("\"'")


def _scan(base, found, prefix=None):
    for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        md = os.path.join(base, d, "SKILL.md")
        if os.path.isfile(md):
            name = _frontmatter(md)[0] or d
            found.setdefault(name, os.path.join(base, d))
            if prefix:
                found.setdefault(f"{prefix}:{name}", os.path.join(base, d))


def _json(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def _plugin_roots():
    home = os.path.expanduser("~/.claude")
    plugins = _json(os.path.join(home, "plugins", "installed_plugins.json"), {}).get("plugins") or {}
    enabled = _json(os.path.join(home, "settings.json"), {}).get("enabledPlugins") or {}
    markets = _json(os.path.join(home, "plugins", "known_marketplaces.json"), {})
    for key, entries in plugins.items():
        if enabled.get(key) is False or not entries:
            continue
        name, _, market = key.partition("@")
        src = (markets.get(market) or {}).get("source") or {}
        # a directory marketplace is read from its folder, not from the cache copy
        root = src.get("path") if src.get("source") == "directory" else entries[0].get("installPath")
        if root:
            yield name, root


def installed(repo=None):
    """Skill name (and `plugin:name`) → absolute skill dir: user skills, the target repository's (unless it is
    in private_roots), then plugins'. Never the session's cwd: it is often the user's private notes folder."""
    found = {}
    _scan(os.path.expanduser("~/.claude/skills"), found)
    root = packet.toplevel(repo) if repo else None
    if root and not packet.is_private(root, config.load()):
        _scan(os.path.join(root, ".claude", "skills"), found)
    for plugin, root in _plugin_roots():
        _scan(os.path.join(root, "skills"), found, plugin)
    return found


def _never(name, path, cfg_never):
    names = {name, name.split(":")[-1], os.path.basename(path)}
    if names & (set(cfg_never) | {n.split(":")[-1] for n in cfg_never}):
        return "in worker_skills.never"
    if os.path.realpath(path).startswith(os.path.realpath(OWN_SKILLS) + os.sep):
        return "argon-bridge's own skill"
    base = name.split(":")[-1]
    if _MEMORY.match(base):
        return "memory or notes skill"
    if _OUTSIDE.search(base.replace("-", " ")) or _OUTSIDE.search(_frontmatter(os.path.join(path, "SKILL.md"))[1]):
        return "acts on the outside world"
    return None


def allowed(names, repo=None):
    """(skill dirs given to Argon, [(name, reason)] refused): Claude's picks plus config `always`, minus the never-list."""
    cfg = config.load().get("worker_skills") or {}
    found = installed(repo)
    given, refused = [], []
    for name in list(names) + [n for n in cfg.get("always") or [] if n not in names]:
        path = found.get(name)
        reason = "not installed" if not path else _never(name, path, cfg.get("never") or [])
        if reason:
            refused.append((name, reason))
        elif path not in given:
            given.append(path)
    return given, refused


def deliver(dirs, worktree):
    """Copies the skills to <worktree>/.agents/skills/<name>/, never over a skill the repository has there;
    returns the paths created (relative to the worktree) for the caller to keep out of git."""
    made = []
    for d in dirs:
        rel = os.path.join(".agents", "skills", os.path.basename(d))
        if not os.path.exists(os.path.join(worktree, rel)):
            shutil.copytree(d, os.path.join(worktree, rel), ignore=shutil.ignore_patterns(".git"))
            made.append(rel.replace(os.sep, "/"))
    return made
