"""The few things that differ between Linux/macOS and Windows. Everything else in the plugin is portable."""
import contextlib
import os
import shutil
import signal
import subprocess
import sys

WINDOWS = os.name == "nt"


def pid_alive(pid):
    """Whether process `pid` still runs. Never signals it: on Windows os.kill would terminate it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True  # a malformed id is left alone
    if WINDOWS:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return k32.GetLastError() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM: the process exists
    return True


@contextlib.contextmanager
def locked(path):
    """Holds an exclusive lock on `path` (created if missing) against other processes for the with-block."""
    with open(path, "a+") as f:
        if WINDOWS:
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # gives up after about 10 s: try again
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)  # released when the file closes
            yield


def detached():
    """Popen keyword arguments for a worker that outlives the hook that starts it."""
    if WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def has_children(pid):
    """Whether `pid` has a running child: agy running a command, so its silence is that command's, not a hang.
    shortcut: Windows always says no (a long command there can be stopped as stalled); a WMI query if it matters."""
    if WINDOWS:
        return False
    try:
        return subprocess.run(["pgrep", "-P", str(pid)], capture_output=True).returncode == 0
    except OSError:  # no pgrep: as before, silence alone decides
        return False


def kill_tree(pid):
    """Stops a detached worker (compat.detached) and what it started, agy included."""
    if WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    for kill in (os.killpg, os.kill):  # its own process group, else the process alone
        try:
            kill(int(pid), signal.SIGTERM)
            return
        except (OSError, TypeError, ValueError):
            continue


def stop_agy(pid):
    """Stops an agy started in a session of its own (gateway._stream) and the commands it runs, when `pid` is still
    an agy: a pid from a dead worker's marker may belong to another process by now. Windows: agy is in the worker's
    tree there, so kill_tree of the worker reaches it."""
    if WINDOWS or not pid:
        return
    try:
        args = subprocess.run(["ps", "-o", "args=", "-p", str(int(pid))], capture_output=True, text=True).stdout
    except (OSError, ValueError):
        return
    if "agy" in os.path.basename(args.split(" ")[0]) or "/agy" in args.split(" ")[:2][-1]:
        kill_group(pid)


def kill_group(pgid):
    """SIGTERM to a process group: what agy left running after it exited (never a lone pid, which may be reused)."""
    if not WINDOWS:
        try:
            os.killpg(int(pgid), signal.SIGTERM)
        except (OSError, ValueError):
            pass


def watchdog(seconds, first=None):
    """Stops this worker and what it started after `seconds`, before its marker is reaped as stale: a stale
    marker's pid may belong to another process by then, so the reaper never kills, the worker does.
    `first`: called before, for what is outside this worker's process group (agy, gateway._stream)."""
    import threading

    def fire():
        if first:
            try:
                first()
            except Exception:  # the worker must stop anyway
                pass
        kill_tree(os.getpid())
    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()


def agy_path():
    return shutil.which("agy")


def agy_cmd():
    """argv prefix that runs agy; a Python script (the tests' stand-in) is run through this interpreter,
    since Windows cannot execute it directly. Any other agy (a binary, a node or shell wrapper) runs as is."""
    path = agy_path() or "agy"
    try:
        with open(path, "rb") as f:
            first = f.readline(200)
    except OSError:
        first = b""
    return [sys.executable, path] if first.startswith(b"#!") and b"python" in first else [path]


def unlink(path):
    """Removes a link `link` made, never what it points at (a junction is removed with rmdir, not walked)."""
    if WINDOWS:  # a junction, even one whose target is gone (isdir is then False), goes with rmdir
        try:
            os.rmdir(path)
            return
        except OSError:
            pass
    os.unlink(path)


def link(src, dst):
    """Makes `dst` point at `src`: a symlink, or where Windows refuses one (no developer mode), a junction for a
    folder and a copy for a file."""
    try:
        os.symlink(src, dst, target_is_directory=os.path.isdir(src))
        return
    except OSError:
        if not WINDOWS:
            raise
    if os.path.isdir(src):
        p = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src], capture_output=True, text=True)
        if p.returncode != 0:
            raise OSError(f"mklink /J {dst}: {(p.stderr or p.stdout).strip()[:200]}")
    else:
        shutil.copy2(src, dst)
