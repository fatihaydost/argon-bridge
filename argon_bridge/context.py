"""The context packet of a delegated job (copied text, never a live folder) and the per-repo Argon notes."""
import hashlib
import os

from . import packet, state

MAX_FILE = 100_000
MAX_NOTES = 40


def notes_path(repo):
    return os.path.join(state.dir(), "notes", hashlib.sha1(repo.encode()).hexdigest()[:16] + ".md")


def notes(repo):
    try:
        with open(notes_path(repo)) as f:
            return [line[2:] for line in f.read().splitlines() if line.startswith("- ")]
    except OSError:
        return []


def add_note(repo, line):
    """One line about a mistake Argon is likely to repeat in this repo; no duplicates, newest MAX_NOTES kept."""
    line = " ".join(line.split())
    kept = [n for n in notes(repo) if n != line] + [line] if line else notes(repo)
    os.makedirs(os.path.dirname(notes_path(repo)), exist_ok=True)
    with open(os.open(notes_path(repo), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("".join(f"- {n}\n" for n in kept[-MAX_NOTES:]))


def build(paths, repo):
    """(markdown, skipped paths): each readable file as `# <path>`; missing, unreadable and
    `remote_allowed: false` files are skipped, long ones truncated; the repo's Argon notes at the end."""
    parts, skipped = [], []
    for p in paths:
        try:
            with open(p, errors="replace") as f:
                text = f.read(MAX_FILE + 1)
        except OSError:
            skipped.append(p)
            continue
        if packet.remote_forbidden(text):
            skipped.append(p)
            continue
        if len(text) > MAX_FILE:
            text = text[:MAX_FILE] + "\n\n[truncated: the file is longer]"
        parts.append(f"# {p}\n\n{text}\n")
    lines = notes(repo)
    if lines:
        parts.append("# Argon notes (mistakes to avoid)\n\n" + "".join(f"- {n}\n" for n in lines))
    return "\n".join(parts), skipped
