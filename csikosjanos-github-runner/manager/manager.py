#!/usr/bin/env python3
"""GitHub runner manager: one container, N self-hosted runner processes, a web UI.

Design (see ../README.md):
- Config (no secrets) lives in DATA/config.json; each runner's PAT lives in
  DATA/secrets/<id>.pat (root-only, 0600). The API never returns a PAT.
- Each runner is a Runner.Listener process in its own copy of the runner
  dist (RUNNERS/<id>), running as its own unprivileged uid, so jobs cannot
  read the PATs or each other's runner credentials.
- The PAT is only used here, to mint short-lived registration/removal tokens.
  Those reach Runner.Listener via an env var (not argv), so `ps` shows nothing.
- Changing a runner restarts only that runner's process.

Python stdlib only.
"""
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("MANAGER_DATA", "/data")
LEGACY_ENV = os.environ.get("MANAGER_LEGACY_ENV", "/legacy/.env")
RUNNER_DIST = os.environ.get("RUNNER_DIST", "/actions-runner")
RUNNERS = os.environ.get("RUNNERS_DIR", "/runners")
GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
GITHUB_URL = os.environ.get("GITHUB_URL", "https://github.com")
PORT = int(os.environ.get("PORT", "8080"))
APP_PROXY_HOST = os.environ.get("APP_PROXY_HOST", "csikosjanos-github-runner_app_proxy_1")
UID_BASE = 20000
AS_ROOT = os.geteuid() == 0

CONFIG = os.path.join(DATA, "config.json")
SECRETS = os.path.join(DATA, "secrets")
HERE = os.path.dirname(os.path.abspath(__file__))
REG_FILES = (".runner", ".credentials", ".credentials_rsaparams")

# Anything that looks like a GitHub token is masked before it is logged.
TOKEN_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ORG_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# GitHub adds the self-hosted, OS and architecture labels (x64 / ARM64) by
# itself, so the default only adds "umbrel". Hard-coding "x64" here would
# mislabel runners on ARM64 boxes (Raspberry Pi).
DEFAULT_LABELS = "umbrel"
# 1.x (amd64 only) registered with these; the migration keeps them so existing
# workflows still match the imported runner.
LEGACY_LABELS = "self-hosted,linux,x64,umbrel"
FIELDS = ("name", "scope", "target", "labels", "group", "ephemeral", "enabled")


def redact(text):
    return TOKEN_RE.sub("***", text)


def log(msg):
    print(redact(msg), flush=True)


def write_private(path, text):
    """Write a file atomically with mode 0600."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- config

def validate(data, existing=None, others=()):
    """Return a clean runner dict built from user input, or raise ValueError."""
    r = dict(existing or {})
    for key in FIELDS:
        if key in data:
            r[key] = data[key]
    r["name"] = str(r.get("name", "")).strip()
    r["scope"] = r.get("scope") or "org"
    r["target"] = str(r.get("target", "")).strip()
    labels = r.get("labels", DEFAULT_LABELS)
    if isinstance(labels, list):
        labels = ",".join(labels)
    r["labels"] = ",".join(l.strip() for l in str(labels).split(",") if l.strip())
    r["group"] = str(r.get("group") or "default").strip()
    r["ephemeral"] = bool(r.get("ephemeral", False))
    r["enabled"] = bool(r.get("enabled", True))

    if not NAME_RE.match(r["name"]):
        raise ValueError("name: 1-64 chars of letters, digits, . _ -")
    if any(o["name"] == r["name"] for o in others):
        raise ValueError("name: already used by another runner")
    if r["scope"] not in ("org", "repo"):
        raise ValueError("scope: must be 'org' or 'repo'")
    if not (ORG_RE if r["scope"] == "org" else REPO_RE).match(r["target"]):
        raise ValueError("target: expected 'org'" if r["scope"] == "org" else "target: expected 'owner/repo'")
    if r["labels"] and not all(LABEL_RE.match(l) for l in r["labels"].split(",")):
        raise ValueError("labels: comma-separated, letters/digits/. _ -")
    if not (0 < len(r["group"]) <= 64) or any(ord(c) < 32 for c in r["group"]):
        raise ValueError("group: 1-64 printable chars")
    return r


def validate_pat(pat):
    pat = str(pat or "").strip()
    if not pat or len(pat) > 255 or any(c.isspace() for c in pat):
        raise ValueError("pat: required, no spaces")
    return pat


def read_env_file(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


class Store:
    """config.json + secrets/<id>.pat. Callers hold Store.lock for writes."""

    def __init__(self):
        self.lock = threading.RLock()
        os.makedirs(SECRETS, exist_ok=True)
        os.chmod(DATA, 0o700)
        os.chmod(SECRETS, 0o700)
        self.cfg = {"runners": []}
        if os.path.exists(CONFIG):
            with open(CONFIG) as f:
                self.cfg = json.load(f)
        self.runners = self.cfg["runners"]

    def migrate(self):
        """Import the 1.x app-data/.env as runner #1, once.

        Runs in the one-shot `migrate` service, the only container that can
        see app-data/.env. The .env and exports.sh are only read, never
        changed, so downgrading to 1.x keeps working.
        """
        self.cfg["legacy_env_present"] = os.path.exists(LEGACY_ENV)
        if not self.cfg.get("migrated"):
            self.cfg["migrated"] = True
            self._import_env()
        self.save()

    def _import_env(self):
        env = read_env_file(LEGACY_ENV)
        if not (env.get("ORG_NAME") and env.get("ACCESS_TOKEN")):
            log("migrate: no 1.x .env with ORG_NAME/ACCESS_TOKEN; nothing to import")
            return
        if self.runners:
            return
        # 1.x hard-coded everything but ORG_NAME/ACCESS_TOKEN in its compose
        # file; keep exactly those values so workflows see the same runner.
        r = validate({"name": "rozsa-umbrel", "scope": "org", "target": env["ORG_NAME"],
                      "labels": LEGACY_LABELS, "group": "default",
                      "ephemeral": False, "enabled": True})
        r["id"] = 1
        self.runners.append(r)
        self.set_pat(r, env["ACCESS_TOKEN"])
        log("migrated legacy .env as runner 1 (%s)" % r["name"])

    def save(self):
        write_private(CONFIG, json.dumps(self.cfg, indent=2))

    def get(self, rid):
        return next((r for r in self.runners if r["id"] == rid), None)

    def next_id(self):
        return max([r["id"] for r in self.runners] + [0]) + 1

    def set_pat(self, r, pat):
        write_private(os.path.join(SECRETS, "%d.pat" % r["id"]), validate_pat(pat))
        r["pat_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def read_pat(self, r):
        with open(os.path.join(SECRETS, "%d.pat" % r["id"])) as f:
            return f.read().strip()

    def delete_pat(self, r):
        try:
            os.remove(os.path.join(SECRETS, "%d.pat" % r["id"]))
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- GitHub

def github_token(pat, r, kind):
    """kind = 'registration' | 'remove'. Returns a short-lived token."""
    base = "orgs/%s" % r["target"] if r["scope"] == "org" else "repos/%s" % r["target"]
    req = urllib.request.Request(
        "%s/%s/actions/runners/%s-token" % (GITHUB_API, base, kind),
        method="POST",
        headers={"Authorization": "Bearer " + pat, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "umbrel-runner-manager"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["token"]
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("message", "")
        except Exception:
            msg = ""
        finally:
            e.close()
        raise RuntimeError("GitHub %s-token request failed: HTTP %d %s" % (kind, e.code, msg)) from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("GitHub %s-token request failed: %s" % (kind, e)) from None


# ---------------------------------------------------------------- runners

def prepare_runners_root():
    """Create RUNNERS so each runner uid can use its own subdirectory.

    0755, not 0711: Runner.Listener refuses to start unless it can READ every
    directory above its own ("Permission to read the directory contents is
    required for '/runners/1' and each directory up the hierarchy").
    Isolation still holds: others only see the numeric dir names, and each
    runner dir is 0700, owned by that runner's uid.
    """
    os.makedirs(RUNNERS, exist_ok=True)
    os.chmod(RUNNERS, 0o755)


class RunnerProc:
    """Supervises one runner: configure, run, restart with backoff, stop."""

    def __init__(self, store, r):
        self.store, self.id = store, r["id"]
        self.status, self.job, self.error = "stopped", None, None
        self.logs = deque(maxlen=300)
        self.proc = None
        self.stop_ev = threading.Event()
        self.thread = None
        self.dir = os.path.join(RUNNERS, str(self.id))
        self.uid = UID_BASE + self.id

    def note(self, line):
        line = redact(line.rstrip())
        self.logs.append(time.strftime("%H:%M:%S ") + line)
        log("[runner %d] %s" % (self.id, line))

    # --- process plumbing
    def _spawn(self, args, env_extra=None):
        env = {"HOME": self.dir, "LANG": "C.UTF-8",
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
        env.update(env_extra or {})
        kw = {}
        if AS_ROOT:
            kw = {"user": self.uid, "group": self.uid, "extra_groups": []}
        return subprocess.Popen(
            [os.path.join(self.dir, "bin", "Runner.Listener")] + args, cwd=self.dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True, **kw)

    def _run_logged(self, args, env_extra=None):
        p = self._spawn(args, env_extra)
        with p.stdout:
            for line in p.stdout:
                self.note(line)
        return p.wait()

    def _prepare(self):
        if AS_ROOT:
            subprocess.run(["groupadd", "-f", "-g", str(self.uid), "runner%d" % self.id], check=True)
            try:
                pwd.getpwuid(self.uid)
            except KeyError:
                subprocess.run(["useradd", "-M", "-d", self.dir, "-s", "/bin/bash",
                                "-u", str(self.uid), "-g", str(self.uid), "runner%d" % self.id], check=True)
        if not os.path.isdir(self.dir):
            self.note("copying runner files")
            shutil.copytree(RUNNER_DIST, self.dir, symlinks=True)
        if AS_ROOT:
            subprocess.run(["chown", "-R", "%d:%d" % (self.uid, self.uid), self.dir], check=True)
        os.chmod(self.dir, 0o700)

    def configured(self):
        return os.path.exists(os.path.join(self.dir, ".runner"))

    def _configure(self, r):
        self.status = "configuring"
        token = github_token(self.store.read_pat(r), r, "registration")
        url = "%s/%s" % (GITHUB_URL, r["target"])
        args = ["configure", "--unattended", "--replace", "--disableupdate",
                "--url", url, "--name", r["name"], "--labels", r["labels"], "--work", "_work"]
        if r["scope"] == "org":
            args += ["--runnergroup", r["group"]]
        if r["ephemeral"]:
            args.append("--ephemeral")
        # Token via env (only readable by this uid), never argv.
        if self._run_logged(args, {"ACTIONS_RUNNER_INPUT_TOKEN": token}) != 0:
            raise RuntimeError("runner configure failed (see log)")

    def unregister(self, r):
        """Best effort: remove this runner's registration from GitHub."""
        if not self.configured():
            return
        try:
            token = github_token(self.store.read_pat(r), r, "remove")
            self._run_logged(["remove"], {"ACTIONS_RUNNER_INPUT_TOKEN": token})
        except Exception as e:
            self.note("unregister failed: %s" % e)
        self.wipe_registration()

    def wipe_registration(self):
        for f in REG_FILES:
            try:
                os.remove(os.path.join(self.dir, f))
            except FileNotFoundError:
                pass

    # --- lifecycle
    def _loop(self):
        backoff = 5
        while not self.stop_ev.is_set():
            r = self.store.get(self.id)
            if not r:
                return
            try:
                self.status, self.error = "starting", None
                self._prepare()
                if not self.configured():
                    self._configure(r)
                if self.stop_ev.is_set():
                    break
                self.status = "connecting"
                self.proc = self._spawn(["run"])
                started = time.time()
                for line in self.proc.stdout:
                    self.note(line)
                    if "Listening for Jobs" in line:
                        self.status, backoff = "idle", 5
                    elif ": Running job: " in line:
                        self.status, self.job = "busy", line.split(": Running job: ", 1)[1].strip()
                    elif " completed with result: " in line:
                        self.status, self.job = "idle", None
                self.proc.stdout.close()
                code = self.proc.wait()
                self.proc = None
                if r["ephemeral"] or (code != 0 and time.time() - started < 60):
                    # Ephemeral: registration is single-use. Quick failure: the
                    # registration was probably deleted on GitHub. Either way
                    # the next loop registers afresh (--replace keeps the name).
                    self.wipe_registration()
                if self.stop_ev.is_set():
                    break
                if code == 0 and r["ephemeral"]:
                    continue
                if time.time() - started > 600:
                    backoff = 5
                raise RuntimeError("runner exited with code %s" % code)
            except Exception as e:
                self.status, self.error, self.job = "error", redact(str(e)), None
                self.note("error: %s (retry in %ds)" % (e, backoff))
                self.stop_ev.wait(backoff)
                backoff = min(backoff * 2, 300)
        self.status, self.job = "stopped", None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_ev.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        p = self.proc
        if p and p.poll() is None:
            self.note("stopping")
            try:
                os.killpg(p.pid, signal.SIGINT)
                p.wait(30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.thread:
            self.thread.join(60)
        self.status, self.job = "stopped", None


class Manager:
    def __init__(self):
        self.store = Store()
        self.procs = {}
        for r in self.store.runners:
            self.procs[r["id"]] = RunnerProc(self.store, r)
            if r["enabled"]:
                self.procs[r["id"]].start()

    def view(self, r):
        p = self.procs.get(r["id"])
        out = {k: r[k] for k in ("id",) + FIELDS}
        out.update(pat_set=os.path.exists(os.path.join(SECRETS, "%d.pat" % r["id"])),
                   pat_updated=r.get("pat_updated"),
                   status=p.status if p else "stopped", job=p.job if p else None,
                   error=p.error if p else None)
        if not r["enabled"] and out["status"] == "stopped":
            out["status"] = "disabled"
        return out

    def list(self):
        return [self.view(r) for r in self.store.runners]

    def create(self, data):
        with self.store.lock:
            r = validate(data, others=self.store.runners)
            pat = validate_pat(data.get("pat"))
            r["id"] = self.store.next_id()
            self.store.set_pat(r, pat)
            self.store.runners.append(r)
            self.store.save()
            self.procs[r["id"]] = p = RunnerProc(self.store, r)
        if r["enabled"]:
            p.start()
        return self.view(r)

    def update(self, rid, data):
        with self.store.lock:
            old = self.store.get(rid)
            if not old:
                raise KeyError(rid)
            new = validate(data, old, [o for o in self.store.runners if o["id"] != rid])
            pat = data.get("pat")
            if pat:
                validate_pat(pat)
        p = self.procs[rid]
        p.stop()
        reg_keys = ("name", "scope", "target", "labels", "group", "ephemeral")
        if pat or any(old[k] != new[k] for k in reg_keys):
            p.unregister(old)  # with the old PAT/target, before they change
        with self.store.lock:
            if pat:
                self.store.set_pat(new, pat)
            old.clear()
            old.update(new)
            self.store.save()
        if new["enabled"]:
            p.start()
        return self.view(old)

    def delete(self, rid):
        r = self.store.get(rid)
        if not r:
            raise KeyError(rid)
        p = self.procs.pop(rid)
        p.stop()
        p.unregister(r)
        shutil.rmtree(p.dir, ignore_errors=True)
        with self.store.lock:
            self.store.runners.remove(r)
            self.store.delete_pat(r)
            self.store.save()

    def action(self, rid, what):
        r = self.store.get(rid)
        if not r:
            raise KeyError(rid)
        p = self.procs[rid]
        if what in ("stop", "restart"):
            p.stop()
        if what in ("start", "stop"):
            with self.store.lock:
                r["enabled"] = what == "start"
                self.store.save()
        if what in ("start", "restart") and r["enabled"]:
            p.start()
        return self.view(r)

    def shutdown(self):
        threads = [threading.Thread(target=p.stop) for p in self.procs.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()


# ---------------------------------------------------------------- HTTP

def gateway_ips():
    """Default-gateway IPs of this container's interfaces.

    On umbrelOS 2.x the app_proxy is not a container: umbreld's in-process app
    gateway on the host connects to this container's IP, so its requests
    arrive from the Docker bridge gateway. Other app containers on
    umbrel_main_network arrive from their own IPs and are refused.
    """
    ips = set()
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if fields[1] == "00000000":  # default route
                    ips.add(socket.inet_ntoa(int(fields[2], 16).to_bytes(4, "little")))
    except (OSError, IndexError, ValueError):
        pass
    return ips


_peers = {"at": 0, "ips": set()}


def allowed_peers():
    """Loopback, the bridge gateway (umbreld) and, on older umbrelOS where
    app_proxy is a sidecar container, that container. Cached for 30s."""
    if time.time() - _peers["at"] > 30:
        ips = {"127.0.0.1"} | gateway_ips()
        # Escape hatch if the gateway ever connects from somewhere else.
        ips |= {i.strip() for i in os.environ.get("EXTRA_ALLOWED_PEERS", "").split(",") if i.strip()}
        try:
            ips.add(socket.gethostbyname(APP_PROXY_HOST))
        except OSError:
            pass
        _peers.update(at=time.time(), ips=ips)
    return _peers["ips"]


class Handler(BaseHTTPRequestHandler):
    manager = None

    def log_message(self, fmt, *args):  # quiet access log; never logs bodies
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _peer_ok(self):
        ip = self.client_address[0].removeprefix("::ffff:")
        if ip in allowed_peers():
            return True
        _peers["at"] = 0  # re-resolve next time, in case an IP changed
        if ip in allowed_peers():
            return True
        self._send(403, {"error": "only reachable through the Umbrel app proxy"})
        return False

    def _route(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        rid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        return parts, rid

    def do_GET(self):
        if not self._peer_ok():
            return
        parts, rid = self._route()
        if not parts:
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if parts == ["api", "runners"]:
            return self._send(200, self.manager.list())
        if parts == ["api", "info"]:
            return self._send(200, {"legacy_env_present": bool(self.manager.store.cfg.get("legacy_env_present"))})
        if len(parts) == 4 and parts[:2] == ["api", "runners"] and parts[3] == "logs" and rid in self.manager.procs:
            return self._send(200, {"lines": list(self.manager.procs[rid].logs)})
        self._send(404, {"error": "not found"})

    def _mutate(self, method):
        if not self._peer_ok():
            return
        # CSRF guard: a cross-site form/fetch cannot set this header without a
        # CORS preflight, which we never grant.
        if self.headers.get("X-Runner-UI") != "1":
            return self._send(403, {"error": "missing X-Runner-UI header"})
        parts, rid = self._route()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            m = self.manager
            if method == "POST" and parts == ["api", "runners"]:
                return self._send(201, m.create(body))
            if method == "PUT" and len(parts) == 3 and rid is not None:
                return self._send(200, m.update(rid, body))
            if method == "DELETE" and len(parts) == 3 and rid is not None:
                m.delete(rid)
                return self._send(200, {"ok": True})
            if method == "POST" and len(parts) == 4 and rid is not None and parts[3] in ("start", "stop", "restart"):
                return self._send(200, m.action(rid, parts[3]))
            self._send(404, {"error": "not found"})
        except KeyError:
            self._send(404, {"error": "no such runner"})
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": str(e)})

    def do_POST(self):
        self._mutate("POST")

    def do_PUT(self):
        self._mutate("PUT")

    def do_DELETE(self):
        self._mutate("DELETE")


def main():
    os.umask(0o077)
    if sys.argv[1:] == ["--migrate"]:
        # Never fail the app start over the import: the runner service
        # depends on this exiting 0.
        try:
            Store().migrate()
        except Exception as e:
            log("migrate failed: %s" % e)
        return 0
    prepare_runners_root()
    Handler.manager = manager = Manager()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    def bye(*_):
        log("shutting down runners")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    log("listening on :%d (%d runners)" % (PORT, len(manager.store.runners)))
    # The UI must never take the runners down: if serving dies, serve again.
    while True:
        try:
            server.serve_forever()
            break  # shutdown() was called
        except Exception as e:
            log("http server error, restarting: %s" % e)
            time.sleep(1)
    manager.shutdown()


if __name__ == "__main__":
    sys.exit(main())
