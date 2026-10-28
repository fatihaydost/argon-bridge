"""What goes to Argon: the user's own words, the change, and the accepted decisions. Never Claude's reasoning."""
import os
import re
import shlex
import shutil
import subprocess
import tempfile

from . import state

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DOC_RE = re.compile(r"(\.(md|txt|rst)$)|(^|/)docs/")
MAX_WORDS = 20_000
MAX_PROMPTS = 10  # the request is in the last few messages; older ones are unrelated conversation


def _git(root, *args):
    p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def toplevel(path):
    out = _git(path, "rev-parse", "--show-toplevel") if os.path.isdir(path) else None
    return os.path.normpath(out.strip()) if out else None  # Windows git prints C:/...


PUNCT = re.compile(r"[;&|()\n]+")  # shlex groups these, e.g. `);` after a subshell
# a heredoc's body is text, not commands; the rest of its first line (`<<EOF > f && git commit`) is kept
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?^\s*\2[ \t]*$", re.S | re.M)


def _native(path):
    """Git Bash on Windows writes C:\\x as /c/x; the rest of the plugin needs the Windows form."""
    if os.name == "nt":
        m = re.match(r"^/([a-zA-Z])(/|$)", path)
        if m:
            path = f"{m.group(1).upper()}:/" + path[m.end():]
    return path


def _tokens(command):
    command = HEREDOC.sub(r"\3", command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace = " \t\r"  # newlines end a command, so they stay as tokens
    lex.wordchars += ":"  # C:/x stays one word, as in the shell
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes
        return command.replace("\n", " \n ").split()


def _git_calls(command, cwd):
    """Every git call in a command, in order, as (dir it runs in, subcommand, its arguments). Honours `cd X && ...`,
    `git -C X` (for that call only), `VAR=1 git` and `/usr/bin/git`. Only words in command position count, so
    `echo git commit` or "git commit" inside a quoted string or heredoc text is not a call."""
    tokens = _tokens(command)
    here, outer = cwd, []
    start = True
    for i, tok in enumerate(tokens):
        if PUNCT.fullmatch(tok):
            for ch in tok:  # a `cd` inside `( ... )` ends with the subshell
                if ch == "(":
                    outer.append(here)
                elif ch == ")" and outer:
                    here = outer.pop()
            start = True
            continue
        if tok.endswith(";"):  # only from the split() fallback
            start = True
            continue
        tok = tok.lstrip("(")
        if not start or re.match(r"\w+=", tok):  # skip arguments and `VAR=1` prefixes
            continue
        start = False
        if tok == "cd" and i + 1 < len(tokens):
            here = os.path.join(here, _native(os.path.expanduser(tokens[i + 1])))
        elif tok == "git" or tok.endswith("/git"):
            where, j = here, i + 1
            while j + 1 < len(tokens) and tokens[j] in ("-C", "-c"):
                if tokens[j] == "-C":
                    where = os.path.join(where, _native(os.path.expanduser(tokens[j + 1])))
                j += 2
            args = []
            for a in tokens[j + 1:]:
                if PUNCT.fullmatch(a):
                    break
                args.append(a.rstrip(";"))
                if a.endswith(";"):
                    break
            if j < len(tokens):
                yield where, tokens[j], args


def _commit_call(command, cwd="."):
    return next((c for c in _git_calls(command, cwd) if c[1] == "commit"), None)


def is_commit(command):
    return _commit_call(command) is not None


def target_repo(command, cwd):
    """Git root a `git commit` command acts on."""
    call = _commit_call(command, cwd)
    return toplevel(call[0] if call else cwd)


def staging(command, cwd, root, every=False):
    """The `git add` runs a commit command will do before it commits, as [(dir, args)]; every=True: before its
    last commit, for `git commit -m a && git add b && git commit -m b`."""
    runs = []
    for where, sub, args in _git_calls(command, cwd):
        if sub == "commit":
            if _stages_all(args):
                runs.append((root, ["-u"]))  # `commit -a` stages tracked files only
            if not every:
                break
            continue
        if sub == "add":
            runs.append((where, args))
    return runs


_VALUE_FLAGS = ("-m", "-F", "-C", "-c", "-t", "--message", "--file", "--author", "--date", "--template",
                "--fixup", "--squash", "--reuse-message", "--reedit-message")


def _stages_all(args):
    """`-a`/`--all` among a commit's own options; a message like "-a" after -m or -qm is not one."""
    takes_value = False
    for a in args:
        if takes_value:
            takes_value = False
            continue
        if a == "--all":
            return True
        if a in _VALUE_FLAGS:
            takes_value = True
        elif re.fullmatch(r"-[a-zA-Z]+", a):
            # in `-am`, letters after a value-taking one (m, F, C, c, t) are its value; at the end, the next arg is
            head, value_letter, rest = re.match(r"([^mFCct]*)([mFCct]?)(.*)", a[1:]).groups()
            if "a" in head:
                return True
            takes_value = bool(value_letter) and not rest
    return False


def commit_all(command):
    call = _commit_call(command)
    return bool(call) and _stages_all(call[2])


def code_diff(root, since=None, adds=None, paths=None, names=False):
    """Changes since `since` (or HEAD): the staged ones, plus what `adds` ([(dir, git add args)]) would stage.
    adds=[(root, ["-A"])]: everything on disk, untracked files included. The user's real index is never touched.
    paths: only these (relative to root); names: the changed paths only, one per line."""
    base = EMPTY_TREE
    if since == EMPTY_TREE:  # a failed review of a repository's first commit: everything (hooks._unreviewed)
        pass
    elif since and _git(root, "merge-base", "--is-ancestor", since, "HEAD") is not None:
        base = since  # not an ancestor (branch switch, rebase): HEAD, so another branch's changes stay out
    elif _git(root, "cat-file", "-e", "HEAD^{commit}") is not None:
        base = "HEAD"
    diff = ["diff", "--cached", *(["--name-only"] if names else []), base,
            *(["--", *(":(literal)" + p for p in paths)] if paths is not None else [])]
    if not adds:
        return _git(root, *diff) or ""
    with tempfile.TemporaryDirectory() as tmp:
        index = os.path.join(tmp, "index")
        real = (_git(root, "rev-parse", "--path-format=absolute", "--git-path", "index") or "").strip()
        if os.path.exists(real):
            shutil.copy2(real, index)  # its time too: git trusts a file's stat only when the index is newer
        env = {**os.environ, "GIT_INDEX_FILE": index}
        for where, args in adds:
            try:
                ok = subprocess.run(["git", "add", *args], cwd=where, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True).returncode == 0
            except OSError:  # `cd $VAR` or a dir that does not exist yet
                ok = False
            if not ok:  # e.g. an unexpanded $VAR path: review everything rather than too little
                subprocess.run(["git", "-C", root, "add", "-A"], env=env, capture_output=True)
                break
        p = subprocess.run(["git", "-C", root, *diff], env=env, capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else ""


def runs(root, shas):
    """Commits `shas` (oldest first) as [[base, last]] spans: one per stretch where each is the next one's parent.
    A commit no longer in HEAD's history (amended, rebased, reset) is left out: its change is gone or in a newer one."""
    shas = [s for s in shas if _git(root, "merge-base", "--is-ancestor", s, "HEAD") is not None]
    out = _git(root, "log", "--no-walk=unsorted", "--format=%H %P", *shas) if shas else None
    spans = []
    for line in (out or "").splitlines():
        sha, *parents = line.split()
        parent = parents[0] if parents else EMPTY_TREE
        if spans and spans[-1][1] == parent:
            spans[-1][1] = sha
        else:
            spans.append([parent, sha])
    return spans


def commit_files(root, base, sha):
    return (_git(root, "diff", "--name-only", base, sha) or "").split("\n")[:-1]


def change_diff(root, shas, adds=None, paths=None):
    """A session's own change: its commits `shas` (a stretch of consecutive ones is one diff, so it reads as the
    commit diff it was) and, with `adds`, what they would stage on top of HEAD, limited to `paths` (None: all)."""
    spans, tail = runs(root, shas), "HEAD"
    if adds is not None and spans and spans[-1][1] == (_git(root, "rev-parse", "HEAD") or "").strip():
        tail, last = spans.pop()  # the uncommitted change continues the last stretch: one diff, as before a commit
        if paths is not None:
            paths = paths + commit_files(root, tail, last)
    out = [_git(root, "diff", base, last) or "" for base, last in spans]
    if adds is not None and paths != []:
        out.append(code_diff(root, tail, adds, paths))
    return "".join(out)


def is_private(root, cfg):
    """Repositories that must never be read by Argon (config `private_roots`)."""
    real = os.path.realpath(root)
    for r in cfg.get("private_roots") or []:
        r = os.path.realpath(os.path.expanduser(r))
        if real == r or real.startswith(r + os.sep):
            return True
    return False


def diff_stats(diff):
    files = re.findall(r"^diff --git a/\S+ b/(\S+)", diff, re.M)
    lines = sum(1 for ln in diff.splitlines()
                if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
    return files, lines


def is_small(diff):
    files, lines = diff_stats(diff)
    if not files:
        return True
    if all(DOC_RE.search(f) for f in files):
        return True
    return len(files) < 2 and lines < 30


def _same_path(a, b):
    return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))


REMOTE_FALSE = re.compile(r"^remote_allowed:\s*[\"']?false[\"']?\s*$", re.M | re.I)


def remote_forbidden(text):
    """True when the note's frontmatter says `remote_allowed: false` (it must never reach Argon)."""
    head = text.split("\n---", 1)[0] if text.startswith("---") else ""
    return bool(REMOTE_FALSE.search(head))


def decisions(root, cfg):
    path = next((v for k, v in (cfg.get("decisions") or {}).items() if _same_path(k, root)), None)
    if not path:
        return ""
    try:
        with open(os.path.expanduser(path)) as f:
            text = f.read()
    except OSError:
        return ""
    return "" if remote_forbidden(text) else text[:MAX_WORDS]


def user_words(session_id):
    data = state.session(session_id)
    prompts = data.get("prompts", [])
    since = (prompts[data.get("review_mark", 0):] or prompts[-1:])[-MAX_PROMPTS:]
    text = "\n\n---\n\n".join(since)
    return text[-MAX_WORDS:]


def user_words_all(session_id):
    """Every captured message of the conversation, for second opinions."""
    text = "\n\n---\n\n".join(state.session(session_id).get("prompts", []))
    return text[-MAX_WORDS:]


def mark_reviewed(session_id):
    data = state.session(session_id)
    data["review_mark"] = len(data.get("prompts", []))
    state.save_session(session_id, data)
