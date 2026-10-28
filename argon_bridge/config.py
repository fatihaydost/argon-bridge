"""User settings in $XDG_CONFIG_HOME/argon-bridge/config.json."""
import json
import os

DEFAULTS = {
    "mode": "standard",      # thorough | standard | off
    "effort": "medium",      # low | medium | high
    "timeout": 900,          # seconds a background code review may take; Argon took 120-500 s
    "plan_glob": "**/plans/*.md",
    "ask_timeout": 400,      # seconds `ask` waits by default; `ask --timeout N` per call, never over `timeout`
    "plan_wait": 120,        # seconds a plan waits for Argon; a longer review finishes in the background
    "decisions": {},         # {"/abs/repo/root": "/abs/path/decisions.md"}
    "private_roots": [],     # repositories Argon must never read, e.g. a personal notes vault
    "manage_agy_permissions": False,  # give each agy run its own permission rules (a temporary project file)
    "worker_skills": {"always": [], "never": []},  # skill names always / never given to a delegated job
    "worker_effort": "medium",  # Argon's effort when it does delegated work
    "worker_timeout": 3600,     # seconds one delegated phase may take
    "stall_seconds": 300,       # seconds a delegated phase may go without any agy event (and no command running) before it is stopped
}
MAX_TIMEOUT = 1800
SYNC_TIMEOUT = 400  # `review` and a plan wait in the foreground: two attempts must fit the 900 s hook limit
WORKER_TIMEOUT = (300, 14400)
CHOICES = {"mode": ("thorough", "standard", "off"), "effort": ("low", "medium", "high"),
           "worker_effort": ("low", "medium", "high"),
           "manage_agy_permissions": ("true", "false")}
BOOLS = ("manage_agy_permissions",)


def path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "argon-bridge", "config.json")


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(path()) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    try:
        cfg["plan_wait"] = max(1, min(int(cfg["plan_wait"]), SYNC_TIMEOUT))
    except (TypeError, ValueError):
        cfg["plan_wait"] = DEFAULTS["plan_wait"]
    try:
        cfg["timeout"] = max(30, min(int(cfg["timeout"]), MAX_TIMEOUT))
    except (TypeError, ValueError):
        cfg["timeout"] = DEFAULTS["timeout"]
    try:
        cfg["ask_timeout"] = max(30, min(int(cfg["ask_timeout"]), cfg["timeout"]))
    except (TypeError, ValueError):
        cfg["ask_timeout"] = min(DEFAULTS["ask_timeout"], cfg["timeout"])
    try:
        cfg["worker_timeout"] = max(WORKER_TIMEOUT[0], min(int(cfg["worker_timeout"]), WORKER_TIMEOUT[1]))
    except (TypeError, ValueError):
        cfg["worker_timeout"] = DEFAULTS["worker_timeout"]
    try:
        cfg["stall_seconds"] = max(1, min(int(cfg["stall_seconds"]), cfg["worker_timeout"]))
    except (TypeError, ValueError):
        cfg["stall_seconds"] = DEFAULTS["stall_seconds"]
    return cfg


def set_value(key, value):
    if key in CHOICES and value not in CHOICES[key]:
        raise ValueError(f"{key} must be one of: {', '.join(CHOICES[key])}")
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting: {key}")
    try:
        with open(path()) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    saved[key] = value == "true" if key in BOOLS else value
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    with open(path(), "w") as f:
        json.dump(saved, f, indent=2)
    return load()
