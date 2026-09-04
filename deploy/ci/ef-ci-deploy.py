#!/usr/bin/python3
"""Root-owned, narrowly scoped release manager. Never runs uploaded code as root.

Installed manually by an administrator, not updated by the CI deployment key.
"""
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
from uuid import uuid4

CURRENT = Path("/opt/easy-finance-current")
RELEASES = Path("/opt/easy-finance-releases")
BACKUPS = Path("/var/backups/easy-finance")
DATABASE = Path("/var/lib/easy-finance/easy_finance.db")
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


def log(message):
    try:
        print(message, flush=True)
    except BrokenPipeError:
        pass  # A disconnected CI client must not interrupt a rollback.


def validate_archive(data, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Invalid revision")
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Archive too large")
    result = {}
    expanded = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for index, item in enumerate(archive):
            if index >= 2000:
                raise ValueError("Too many archive members")
            path = PurePosixPath(item.name)
            if path.is_absolute() or ".." in path.parts or "\\" in item.name:
                raise ValueError("Unsafe archive path")
            name = str(path)
            if name not in {"app", "requirements.txt", "REVISION"} and not name.startswith("app/"):
                raise ValueError("Unexpected archive path")
            if item.isdir() and name.startswith("app"):
                continue
            if not item.isfile() or name == "app" or name in result:
                raise ValueError("Links, special files and duplicate members are forbidden")
            expanded += item.size
            if item.size < 0 or expanded > MAX_EXPANDED:
                raise ValueError("Expanded archive too large")
            with archive.extractfile(item) as source:
                result[name] = source.read(item.size + 1)
            if len(result[name]) != item.size:
                raise ValueError("Truncated archive member")
    if not {"app/main.py", "requirements.txt", "REVISION"}.issubset(result):
        raise ValueError("Incomplete release")
    if result["REVISION"] != (revision + "\n").encode():
        raise ValueError("Revision does not match archive")
    # No uploaded tar member is ever passed to extractall().
    return result


def run(arguments, *, cwd=None, env=None, timeout=600):
    process = subprocess.Popen(arguments, cwd=cwd, env=env, start_new_session=True)
    try:
        code = process.wait(timeout=timeout)
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    if code:
        raise RuntimeError(f"Command failed: {arguments[0]} (exit {code})")


def switch_to(target):
    temporary = CURRENT.with_name(CURRENT.name + ".next-" + uuid4().hex)
    temporary.symlink_to(target)
    os.replace(temporary, CURRENT)


def wait_healthy(revision=None, public=False):
    url = "https://l9k.dev/ef/health" if public else "http://127.0.0.1:8001/health"
    for attempt in range(30):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                payload = json.load(response)
                if payload.get("status") == "ok" and (revision is None or payload.get("revision") == revision):
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Release health check failed: " + url)


def build_release(files, revision):
    import pwd
    if shutil.disk_usage(RELEASES).free < 1024 * 1024 * 1024:
        raise RuntimeError("Less than 1 GiB free; clean old releases manually before deploying")
    release = RELEASES / (revision + "-" + uuid4().hex[:8])
    release.mkdir(mode=0o755)
    for name, content in files.items():
        target = release / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_bytes(content)
        target.chmod(0o644)
    builder = pwd.getpwnam("ef-deploy")
    os.chown(release, builder.pw_uid, builder.pw_gid)
    build_env = {
        "PATH": "/usr/bin:/bin", "HOME": "/var/lib/ef-deploy", "LANG": "C.UTF-8",
        "PIP_CACHE_DIR": "/var/cache/easy-finance-ci", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    python = str(release / ".venv/bin/python")
    prefix = ["/usr/sbin/runuser", "-u", "ef-deploy", "--"]
    try:
        run(prefix + ["/usr/bin/python3", "-m", "venv", str(release / ".venv")], env=build_env)
        run(prefix + [python, "-m", "pip", "install", "-r", str(release / "requirements.txt")], env=build_env)
        run(prefix + [python, "-m", "pip", "check"], env=build_env)
        run(prefix + [python, "-c", "from app.ocr import get_engine; get_engine(); from app.main import app; print('Release imports and offline OCR ready')"],
            cwd=release, env=build_env, timeout=90)
    finally:
        # Uploaded code/build steps never run as root; published releases are immutable to both service users.
        for parent, directories, filenames in os.walk(release, followlinks=False):
            os.chown(parent, 0, 0, follow_symlinks=False)
            for name in directories + filenames:
                os.chown(Path(parent) / name, 0, 0, follow_symlinks=False)
    return release


def snapshot_database(revision):
    """Called with the app stopped. Never follows a database symlink or restores data automatically."""
    destination = BACKUPS / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + revision[:12])
    destination.mkdir(mode=0o700)
    for source in (DATABASE, Path(str(DATABASE) + "-wal"), Path(str(DATABASE) + "-shm")):
        if not source.exists() and not source.is_symlink():
            continue
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RuntimeError("Database is not a regular file")
            target = destination / source.name
            with target.open("xb") as output:
                shutil.copyfileobj(stream, output)
            target.chmod(0o600)
    log("Pre-deployment database snapshot: " + str(destination))


def activate_release(candidate, previous, revision):
    stopped = False
    changed = False
    try:
        stopped = True  # Even a partially failed stop must restart the original service.
        run(["/usr/bin/systemctl", "stop", "easy-finance"], timeout=45)
        snapshot_database(revision)
        switch_to(candidate)
        changed = True
        log("Starting candidate " + revision)
        run(["/usr/bin/systemctl", "start", "easy-finance"], timeout=45)
        wait_healthy(revision)
        wait_healthy(revision, public=True)
    except BaseException:
        log("Deployment failed; restoring the previous code version (database is not rolled back)")
        if changed:
            with contextlib.suppress(Exception):
                run(["/usr/bin/systemctl", "stop", "easy-finance"], timeout=45)
            switch_to(previous)
        if stopped:
            run(["/usr/bin/systemctl", "start", "easy-finance"], timeout=45)
            wait_healthy()
        raise


def deploy(revision):
    import fcntl
    if os.geteuid() != 0:
        raise RuntimeError("Run through the approved sudo wrapper")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Invalid revision")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    def interrupted(_signum, _frame):
        raise RuntimeError("Deployment interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    if not CURRENT.is_symlink():
        raise RuntimeError("Current release pointer is not initialized")
    previous = CURRENT.resolve(strict=True)
    if previous != Path("/opt/easy-finance") and previous.parent != RELEASES:
        raise RuntimeError("Unexpected current release path")
    with open("/run/lock/easy-finance-deploy.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log("Receiving release " + revision)
        # Bound both bytes and receive time; the dedicated key cannot write arbitrary server paths.
        def timed_out(_signum, _frame):
            raise TimeoutError("Archive upload timed out")
        signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(120)
        try:
            data = sys.stdin.buffer.read(MAX_ARCHIVE + 1)
        finally:
            signal.alarm(0)
        files = validate_archive(data, revision)
        del data
        log("Building candidate without modifying the running service")
        candidate = build_release(files, revision)
        run(["/usr/bin/systemctl", "is-active", "--quiet", "easy-finance"], timeout=10)
        wait_healthy()
        activate_release(candidate, previous, revision)
        log("DEPLOY_OK " + revision)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected exactly one commit SHA")
        deploy(sys.argv[1])
    except Exception as exc:
        log("DEPLOY_FAILED: " + str(exc))
        sys.exit(1)
