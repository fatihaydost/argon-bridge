"""The only place that talks to Argon. Everything else is model-agnostic."""
from __future__ import annotations  # `X | None` on Python 3.9 (macOS system python3)

import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field

from . import compat, permissions, schemas, state
from .prompts import DIFF_CUT

MODEL = "gemini-4-argon"
LOWER = {"high": "medium", "medium": "low", "low": "low"}
QUOTA_COOLDOWN = 1800  # shortcut: real reset window is unpublished; tune once a real quota error is seen

_QUOTA = re.compile(r"quota|rate.?limit|429|exhaust|limit reached|too many requests", re.I)
_LOGIN = re.compile(r"log.?in|sign.?in|auth|credential|unauthori", re.I)
_TIMEOUT = re.compile(r"time.?out|deadline|stream was interrupted|turn in progress", re.I)
RUNNING_AGY = None  # the pid of the worker's agy while _stream runs it, for the watchdog (compat.stop_agy)
WORK = None  # the temp folder of the run in progress: its packet is private code
NOTE_EVERY = 10  # seconds between two notes of a worker's latest agy event (the marker is a file)
RAW_KEEP, RAW_DAYS, RAW_TAIL = 200, 7, 2_000_000  # agy's raw output: files kept, their age, bytes of each
JSON_ONLY = "\n\nAnswer with only one JSON object that fits the given schema, nothing else: an answer in prose " \
    "cannot be read."


@dataclass
class Result:
    ok: bool
    data: dict | None = None
    error: str | None = None  # not_installed|not_logged_in|quota|cooldown|timeout|stalled|empty|malformed|agy_error|no_guard
    detail: str = ""
    duration: float = 0.0
    usage: dict = field(default_factory=dict)
    raw: str = ""  # the file holding agy's raw output


class Invalid(ValueError):
    pass


def clip(value, schema):
    """Check Argon's answer against the schema ourselves: types, enums, lengths. Never trust the remote side.
    Array items that do not fit are dropped; a wrong top-level shape raises Invalid."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise Invalid("expected object")
        props = schema.get("properties", {})
        out = {k: clip(value[k], props[k]) for k in props if k in value}
        missing = [k for k in schema.get("required", []) if k not in out]
        if missing:
            raise Invalid(f"missing {missing}")
        return out
    if t == "array":
        if not isinstance(value, list):
            raise Invalid("expected array")
        kept = []
        for v in value:
            try:
                kept.append(clip(v, schema.get("items", {})))
            except Invalid:
                continue
        return kept[: schema.get("maxItems", 20)]
    if t == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise Invalid("expected integer")
    if t == "string":
        if not isinstance(value, (str, int, float)):
            raise Invalid("expected string")
        value = str(value)
        if "enum" in schema and value not in schema["enum"]:
            raise Invalid(f"{value!r} not in enum")
        return value[: schema.get("maxLength", 500)]
    raise Invalid("unexpected schema")


def _call(prompt, packet, cwd, schema, effort, timeout, grace, extra, **kw):
    start = time.time()
    result = _call_once(prompt, packet, cwd, schema, effort, timeout, grace, extra, **kw)
    # agy's --print-timeout ends as SUCCESS with an empty or half answer, or as an error: Argon was still working
    late = time.time() - start >= timeout - min(5, timeout / 20)
    if result.error in ("empty", "malformed", "agy_error") and late:
        result.error, result.detail = "timeout", f"no answer within {timeout}s ({result.error}: {result.detail})"[:300]
    return result


def _text(out):
    return out.decode(errors="replace") if isinstance(out, bytes) else out or ""


def _keep_raw(kind, stdout, stderr):
    """agy's raw output, to see later why an answer was empty or broken. It may hold code or secrets: 0600."""
    d = os.path.join(state.dir(), "raw")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        old = sorted(os.listdir(d))  # names start with the time
        for i, name in enumerate(old):
            p = os.path.join(d, name)
            if i <= len(old) - RAW_KEEP or os.path.getmtime(p) < time.time() - RAW_DAYS * 86400:
                os.remove(p)
        fd, path = tempfile.mkstemp(prefix=time.strftime("%Y%m%d-%H%M%S-") + kind + "-", suffix=".txt", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"# stderr (tail)\n{_text(stderr)[-20_000:]}\n# stdout (tail)\n{_text(stdout)[-RAW_TAIL:]}\n")
        return path
    except OSError:  # a diagnostic aid must not fail the call
        return ""


def _diff_spans(packet):
    """{path: [(first, last)]}: the new-side line ranges of each hunk of the git diff in a packet."""
    spans, cur = {}, None
    for ln in packet.splitlines():
        if ln.startswith("diff --git "):
            cur = None
        elif ln.startswith("+++ "):
            cur = spans.setdefault(ln[6:], []) if ln.startswith("+++ b/") else None
        elif cur is not None and ln.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", ln)
            if m:
                cur.append((int(m[1]), int(m[1]) + max(int(m[2] or 1), 1) - 1))
    return spans


def _norm(text):
    return " ".join(text.split())


def _check_findings(findings, cwd, packet):
    """Marks what Argon cannot back up: `unverified` when the quoted evidence is in neither the file nor the packet
    (Argon has quoted code that does not exist), `origin: pre-existing` when the line is outside the diff's hunks.
    Marked afterwards, not asked for in the schema: Argon's answer form stays as it is."""
    root = os.path.realpath(cwd)
    # a cut diff's later hunks are missing, so a line outside the ones left is not known to be pre-existing
    spans = _diff_spans(packet) if "\ndiff --git " in "\n" + packet and DIFF_CUT not in packet else None
    seen = _norm(packet)
    for f in findings:
        path = os.path.realpath(os.path.join(root, f["file"]))
        text = ""
        if path.startswith(root + os.sep):  # never a file outside the repository, through `..` or a link
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read(RAW_TAIL)
            except OSError:
                pass
        # Argon joins pieces of a long quote with `...`: each piece must be there
        pieces = [_norm(q) for q in re.split(r"\.\.\.|…", f["evidence"]) if _norm(q)]
        if not pieces or any(q not in _norm(text) and q not in seen for q in pieces):
            f["unverified"] = True
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if spans is not None and not any(a <= f["line"] <= b for a, b in spans.get(rel, [])):
            f["origin"] = "pre-existing"


_TOOLS = {"RunCommand": "run_command", "ReadFile": "read_file"}  # denied_actions display_name -> step tool_name


def _denied(stdout, actions):
    """What each of agy's `denied_actions` tried, from the stream: "command: <line>" or "read_file <path>".
    The denied step is the last one of its tool and has no output (an allowed one has); agy shows a `&&` chain
    whole, not the part it refused (probe 24.10). A step that is not in the stream (a subagent's) is not guessed."""
    steps = {}
    for line in stdout.splitlines():
        if '"step_update"' not in line:
            continue
        s = _json_line(line).get("step_update")
        if isinstance(s, dict) and s.get("tool_name"):
            info = s.get("tool_info") or {}
            steps[s.get("step_index", len(steps))] = (s["tool_name"], info.get("parameters") or {}, "output" in info)
    out = []
    for a in actions if isinstance(actions, list) else []:
        name = _TOOLS.get(a.get("display_name"), a.get("action"))
        last = [v for v in steps.values() if v[0] == name][-1:]  # in the order agy began them
        params = last[0][1] if last and not last[0][2] else {}
        if name == "run_command":
            out.append(f"command: {params.get('CommandLine') or '(not shown)'}")
        elif name == "read_file":
            out.append(f"read_file {params.get('Path') or '(path not shown)'}")
        else:
            out.append(f"{a.get('display_name') or name} {json.dumps(params)[:300] if params else '(not shown)'}")
    return out


def _last_result(stdout, stream):
    """The final JSON object agy printed (with stream-json: the `result` event)."""
    for line in reversed(stdout.strip().splitlines()):
        if line.startswith("{"):
            try:
                out = json.loads(line)
            except ValueError:
                continue
            if stream:
                out = out.get("result") if out.get("event") == "result" else None
            if isinstance(out, dict):
                return out
    return None


def _event_name(line):
    """A stream-json line as a short label: the tool or step type and its state, else the event."""
    e = _json_line(line)
    if not e:
        return None
    s = e.get("step_update")
    if isinstance(s, dict):
        return " ".join(str(x) for x in (s.get("tool_name") or s.get("step_type"), s.get("state")) if x)
    return str(e.get("event") or "?")


def _stream(cmd, cwd, limit, stall, on_event):
    """Runs a worker's agy and reads its stream-json as it comes. Stops agy and what it started (a command) when
    no event came for `stall` seconds or the run passed `limit`. Returns (CompletedProcess, error, detail)."""
    # its own process group, so compat.kill_tree reaches the commands it runs (Windows: taskkill /T does)
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace",
                         **({} if compat.WINDOWS else {"start_new_session": True}))
    lines, errs, q = [], [], queue.Queue()  # a thread per pipe: select does not work on Windows pipes

    def pump(src, put):
        for ln in src:
            put(ln)
        put(None)
    readers = [threading.Thread(target=pump, args=(p.stdout, q.put), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, errs.append), daemon=True)]
    for t in readers:
        t.start()
    global RUNNING_AGY
    RUNNING_AGY = p.pid
    note = on_event or (lambda fields: None)
    note({"agy_pid": p.pid})
    start = last = time.time()
    noted, name, error, detail, unsaid = 0.0, "none yet", None, "", False
    try:
        while True:
            now = time.time()
            if now - last >= stall and compat.has_children(p.pid):  # a command runs (a long test): not a hang
                last = now
            if now - last >= stall:
                error, detail = "stalled", f"no event from agy for {int(now - last)}s, silent since: {name}"
            elif now - start >= limit:
                error, detail = "timeout", f"no answer within {limit}s"
            if error:
                compat.kill_tree(p.pid)
                break
            try:
                ln = q.get(timeout=max(0.05, min(1.0, stall - (now - last), limit - (now - start))))
            except queue.Empty:
                # quiet now: the latest event, not one from up to NOTE_EVERY ago (a flag, not times: Windows' clock
                # ticks in ~16 ms, so two events can share one)
                if unsaid:
                    noted, unsaid = last, False
                    note({"last_event": last, "event": name})
                continue
            if ln is None:
                break
            lines.append(ln)
            last = time.time()
            name = _event_name(ln) or name
            if name == "result":  # the answer; a command agy left running may keep the pipe open
                break
            unsaid = True
            if last - noted >= NOTE_EVERY:
                noted, unsaid = last, False
                note({"last_event": last, "event": name})
    finally:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:  # agy did not stop on SIGTERM
            p.kill()
            p.wait()
        compat.kill_group(p.pid)  # a command agy left running after its answer (it holds the pipe open)
        for t, f in zip(readers, (p.stdout, p.stderr)):
            t.join(timeout=5)
            if not t.is_alive():  # closing a pipe a thread still reads would wait for that read
                f.close()
        RUNNING_AGY = None
        note({"agy_pid": None})
    return subprocess.CompletedProcess(cmd, p.returncode, "".join(lines), "".join(e for e in errs if e)), error, detail


_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _json_object(text):
    """The answer as a JSON object: the whole text, else the last fenced ```json block in prose; or None."""
    for candidate in [text.strip()] + _FENCED.findall(text)[::-1]:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


TEMP_PREFIXES, TEMP_AGE = ("argon-bridge-", "argon-ask-"), 12 * 3600  # longer than any run (watchdog: 2 x 4 h)


def on_term(*_):
    """SIGTERM in a background worker (watchdog, reject, kill): stops agy and deletes the packet, then exits at
    once. No `finally` runs, so the job's running marker stays and the reaper reports the change as not reviewed."""
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    compat.stop_agy(RUNNING_AGY)  # a worker's agy, in a session of its own
    if not compat.WINDOWS and os.getpgid(0) == os.getpid():  # a review's agy, in this worker's group
        with contextlib.suppress(OSError):
            os.killpg(os.getpid(), signal.SIGTERM)
    if WORK:
        shutil.rmtree(WORK, ignore_errors=True)
    os._exit(143)


def sweep_temp():
    """Removes the temp folders of runs killed without a signal they could handle (SIGKILL, a power cut): they hold
    review packets, that is private code."""
    root = tempfile.gettempdir()
    try:
        names = [n for n in os.listdir(root) if n.startswith(TEMP_PREFIXES)]
    except OSError:
        return
    for n in names:
        p = os.path.join(root, n)
        try:
            if os.path.isdir(p) and not os.path.islink(p) and time.time() - os.path.getmtime(p) > TEMP_AGE:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _call_once(prompt, packet, cwd, schema, effort, timeout, grace, extra, mode="plan", conversation=None,
               kind="call", stall=300, on_event=None):
    """mode "plan": a read-only review. Any other mode is a worker run: stream-json output read as it comes
    (`stall`, `on_event`: _stream), the report is the answer text parsed as JSON (a resumed turn may repeat the
    last turn's structured_output, agy-probes.md)."""
    worker = mode != "plan"
    global WORK
    sweep_temp()
    work = WORK = tempfile.mkdtemp(prefix="argon-bridge-")
    try:
        packet_path = os.path.join(work, "packet.md")
        schema_path = os.path.join(work, "schema.json")
        with open(packet_path, "w") as f:
            f.write(packet)
        with open(schema_path, "w") as f:
            json.dump(schema, f)
        cmd = [
            *compat.agy_cmd(), "-p", f"{prompt}\n\nThe {'phase' if worker else 'review'} packet is the file {packet_path}. Read it first.",
            "--model", MODEL, "--effort", effort,
            "--output-format", "stream-json" if worker else "json", "--json-schema", schema_path,
            "--mode", mode, "--sandbox",
            "--print-timeout", f"{timeout}s", "--add-dir", work, *extra,
            *(["--conversation", conversation] if conversation else []),
        ]
        if worker:
            proc, error, detail = _stream(cmd, cwd, timeout + grace, stall, on_event)
            if error:
                cid = next((e.get("conversation_id") for e in map(_json_line, proc.stdout.splitlines())
                            if e.get("conversation_id")), None) or conversation
                detail = f"no answer within {timeout}s" if error == "timeout" else detail
                return Result(False, data={"conversation_id": cid} if cid else None, error=error, detail=detail,
                              raw=_keep_raw(kind, proc.stdout, proc.stderr))
        else:
            try:
                proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout + grace)
            except subprocess.TimeoutExpired as e:
                return Result(False, error="timeout", detail=f"no answer within {timeout}s",
                              raw=_keep_raw(kind, e.stdout, e.stderr))
        result = _answer(proc, schema, worker, conversation)
        result.raw = _keep_raw(kind, proc.stdout, proc.stderr)
        return result
    finally:
        WORK = None
        shutil.rmtree(work, ignore_errors=True)


def _json_line(line):
    try:
        e = json.loads(line)
    except ValueError:
        return {}
    return e if isinstance(e, dict) else {}


def _answer(proc, schema, worker, conversation):
    """agy's output as a Result. A worker's failed answer keeps its conversation_id: the turn may have done the work."""
    out = _last_result(proc.stdout, worker)
    if out is None:
        return Result(False, error="agy_error", detail=(proc.stderr or proc.stdout).strip()[:300])
    usage = out.get("usage") or {}
    if out.get("status") != "SUCCESS":
        msg = str(out.get("error") or proc.stderr or "unknown error")
        if _QUOTA.search(msg):
            state.set_cooldown(QUOTA_COOLDOWN)
            # a worker resumes this conversation once the quota is back
            kept = {"conversation_id": out["conversation_id"]} if worker and out.get("conversation_id") else None
            return Result(False, data=kept, error="quota", detail=msg[:300], usage=usage)
        if _LOGIN.search(msg):
            return Result(False, error="not_logged_in", detail=msg[:300], usage=usage)
        if _TIMEOUT.search(msg):
            return Result(False, error="timeout", detail=msg[:300], usage=usage)
        return Result(False, error="agy_error", detail=msg[:300], usage=usage)
    structured = out.get("structured_output")
    meta = {}
    if worker:
        meta = {"conversation_id": out.get("conversation_id") or conversation,
                "denied": _denied(proc.stdout, out.get("denied_actions"))}
        answer = _json_object(str(out.get("response") or ""))
        structured = answer if isinstance(answer, dict) else None if conversation else structured
        if structured is None and meta["denied"]:  # an action neither allowed nor denied ended the turn
            return Result(True, data={"status": "denied", "summary": "Argon's turn ended on an action that is not "
                                      f"allowed: {meta['denied'][0]}"[:500], "changes": [], "commands_run": [],
                                      "failures": [], **meta}, usage=usage)
    if isinstance(structured, dict):
        try:
            data = {**clip(structured, schema), **meta}
            dropped = sum(len(v) - len(data[k]) for k, v in structured.items() if k in schema["properties"]
                          if isinstance(v, list) and isinstance(data.get(k), list))
            detail = f"{dropped} item(s) dropped: did not fit the form" if dropped else ""
            return Result(True, data=data, detail=detail, usage=usage)
        except Invalid as e:
            return Result(False, data=meta or None, error="malformed", detail=f"answer did not fit the form: {e}",
                          usage=usage)
    if not str(out.get("response") or "").strip():
        return Result(False, data=meta or None, error="empty", detail="Argon returned nothing", usage=usage)
    return Result(False, data=meta or None, error="malformed", detail="answer did not follow the required form",
                  usage=usage)


def run(kind, prompt, packet, cwd, schema, effort, timeout, grace=30, rules=None, meta=None):
    """`rules`: agy permission rules for this run; by default Argon may not read secret files.
    `meta`: more fields for the log line (a code review's session and commits)."""
    start = time.time()
    retried = False
    if not compat.agy_path():
        result = Result(False, error="not_installed", detail="agy (Antigravity CLI) is not on PATH")
    elif time.time() < state.cooldown_until():
        result = Result(False, error="cooldown", detail="Argon quota was exhausted recently; skipping until it resets")
    else:
        with permissions.hold(rules or {"deny": permissions.secret_rules(cwd)}, cwd) as extra:
            result = _call(prompt, packet, cwd, schema, effort, timeout, grace, extra or [], kind=kind)
            # retry only if a second try still fits; a timeout is never retried (it would double the wait)
            if result.error in ("empty", "malformed") and time.time() - start < timeout:
                retried = True
                again = (prompt + JSON_ONLY, effort) if result.error == "malformed" else (prompt, LOWER[effort])
                result = _call(again[0], packet, cwd, schema, again[1], timeout, grace, extra or [], kind=kind)
    if result.ok and schema is schemas.FINDINGS:
        _check_findings(result.data["findings"], cwd, packet)
    result.duration = round(time.time() - start, 1)
    findings = len((result.data or {}).get("findings", [])) if result.ok else None
    state.log({"kind": kind, "cwd": cwd, "effort": effort, "error": result.error, "detail": result.detail,
               "duration": result.duration, "usage": result.usage, "findings": findings, "retried": retried,
               "raw": result.raw, **(meta or {})})
    return result


def work(job, phase, prompt, packet, effort, timeout, rules, extra_args, stall=300, on_event=None):
    """One phase of a delegated job: agy edits and runs commands in the job's worktree under `rules`.
    `stall`: seconds without any agy event before the run is stopped as `stalled`; `on_event(fields)` hears
    agy's pid and, every NOTE_EVERY seconds at most, its latest event.
    A phase is long, so it is not run again; a turn that ended without a fitting report is asked once, in the same
    conversation, for the report alone. Result.data = the clipped PHASE_REPORT plus conversation_id and denied."""
    start = time.time()
    if not compat.agy_path():
        result = Result(False, error="not_installed", detail="agy (Antigravity CLI) is not on PATH")
    elif time.time() < state.cooldown_until():
        result = Result(False, error="quota", detail="Argon quota was exhausted recently; waiting for it to reset")
    else:
        # the same project every run: a resumed agy conversation keeps its first turn's project (agy-probes.md)
        # the repo's own agy project rules are carried over (the worktree lives in the state dir)
        with permissions.hold(rules, job["repo"], f"job-{job['id']}") as extra:
            if extra is None:  # the allow/deny lists are the worker's real barrier: no guard, no run
                result = Result(False, error="no_guard", detail="the permission guard is off or failed; "
                                "run `argon-bridge permissions on` before handing work to Argon")
            else:
                result = _call(prompt, packet, job["worktree"], schemas.PHASE_REPORT, effort, timeout, 60,
                               [*extra, *extra_args], mode="accept-edits", conversation=job.get("conversation"),
                               kind="work", stall=stall, on_event=on_event)
                cid = (result.data or {}).get("conversation_id")
                if result.error == "malformed" and cid:
                    result = _call("Do no more work. Report the phase you just worked on." + JSON_ONLY,
                                   "Only the phase report is missing.", job["worktree"], schemas.PHASE_REPORT,
                                   effort, min(timeout, 600), 60, [*extra, *extra_args], mode="accept-edits",
                                   conversation=cid, kind="work", stall=stall, on_event=on_event)
    result.duration = round(time.time() - start, 1)
    state.log({"kind": "work", "cwd": job["worktree"], "job": job["id"], "phase": phase["title"], "effort": effort,
               "error": result.error, "detail": result.detail, "duration": result.duration, "usage": result.usage,
               "status": (result.data or {}).get("status"), "raw": result.raw})
    return result
