# GitHub Runner

Self-hosted **GitHub Actions runners with a web UI**, packaged as an Umbrel app.

- **Image:** `ghcr.io/csikosjanos/github-runner-manager` (built from [`manager/`](./manager) by
  [`.github/workflows/build-github-runner-manager.yml`](../.github/workflows/build-github-runner-manager.yml)),
  based on [`myoung34/github-runner`](https://github.com/myoung34/docker-github-actions-runner)
- **App id:** `csikosjanos-github-runner`
- **Port:** 9200 (web UI, behind the Umbrel login)

## Why an Umbrel app (and not just `docker run`)

On umbrelOS the root filesystem is a wiped-on-OTA overlay, and **umbreld removes
any container it doesn't manage on every boot**. Packaged as an app, umbreld owns
its lifecycle and recreates it on boot, so runners survive reboots and OTA updates.

## Using the UI

Open the app. Each runner card shows its status (`idle` = online and listening,
`busy: <job>`, `error`, `disabled`), scope, labels, and when its token was last
updated. Buttons: Stop/Start (persisted as enabled/disabled), Restart, Logs
(last 300 lines), Edit, Remove (also unregisters it from GitHub).

**Add runner** asks for name, scope (organisation, or one `owner/repo`), labels,
runner group (org only), ephemeral, enabled, and a **new** personal access token:

| Scope | Fine-grained PAT permission |
|---|---|
| Organisation | Organisation → **Self-hosted runners: Read and write** |
| Repository | Repository → **Administration: Read and write** |

Editing a runner restarts only that runner. If the name, target, labels, group,
ephemeral flag or token changed, it is unregistered first and registered again.

Workflows target runners by label:

```yaml
jobs:
  build:
    runs-on: [self-hosted, umbrel]   # array = AND across labels
```

## How the token is protected

- Stored in `app-data/csikosjanos-github-runner/data/secrets/<id>.pat`, mode
  0600, directory 0700, root only. `config.json` next to it holds everything
  else and never the token.
- The API accepts a token on add/edit but never returns it; the UI shows only
  "set, updated <time>". Log lines are masked for anything token-shaped.
- The token never reaches a runner. The manager uses it to request a short-lived
  registration token from GitHub and passes that to the runner through an
  environment variable (readable only by that runner's user, not via `ps`).
- Each runner runs as its own unprivileged user (uid 20000+id) in its own
  directory (mode 0700), so a job can't read the tokens or another runner's
  credentials. Jobs have no sudo (`no-new-privileges`, not in sudoers).
- The UI needs the Umbrel login (`PROXY_AUTH_ADD` left at its default, on).
  The manager also refuses every request (GET included) that doesn't come from
  the app gateway. On umbrelOS 2.x the gateway runs inside umbreld on the host
  and connects to the container's IP, so its requests arrive from the Docker
  bridge gateway. Other app containers on `umbrel_main_network` arrive from
  their own IPs and get a 403. Allowed peers are 127.0.0.1, the container's
  default-gateway IPs (from `/proc/net/route`), and, on older umbrelOS where
  app_proxy is a container, `csikosjanos-github-runner_app_proxy_1`. The list
  is re-resolved every 30 s, or right away when a request is refused.
  Host-network apps, which are host-level already, still count as "the host".
  If a future umbrelOS connects from another address, the UI returns 403 and
  the runners keep running. Add `EXTRA_ALLOWED_PEERS: <ip>` to the `runner`
  service's environment as a stopgap.
- Changes also need an `X-Runner-UI` request header, so other sites can't
  submit forms to it in your browser.
- The old 1.x `.env` is mounted only into the one-shot `migrate` container,
  read-only, for a few seconds per app start. The long-running `runner`
  container doesn't see it. Runner uids (20000+) can't read
  `data/secrets/` (root, 0700/0600), and `.env` is 0600, owned by root/1000.

## Design: one container, N runner processes (no docker.sock)

Two options were considered for starting and stopping individual runners:

1. **One container per runner, created by the UI through the Docker API.** This
   needs `POST /containers/create`. A docker-socket-proxy can limit which
   endpoints are reachable, but it can't limit what gets created (privileged,
   host mounts), so this is still root on the box. The token would also end up
   in each container's environment, visible in `docker inspect`.
2. **One supervisor container running N `Runner.Listener` processes**
   (chosen). The image is `myoung34/github-runner` (the same job toolchain as
   1.x) plus `manager/manager.py`, which is Python stdlib only (about 550 lines)
   and serves `manager/index.html`. Each runner gets a copy of `/actions-runner`
   in `/runners/<id>` and its own user. Stopping or restarting a runner signals
   only that process. It needs no Docker access at all.

Runner state outside `/data` is rebuilt on container start: each runner
re-registers with `--replace`, which keeps the same name in GitHub. 1.x did the
same thing.

## Upgrading from 1.x (migration)

1.x read `ORG_NAME` / `ACCESS_TOKEN` from `app-data/csikosjanos-github-runner/.env`,
loaded into compose by an app-data `exports.sh`. umbreld's app-script doesn't
pass an env file to `docker compose`, but it does source
`${UMBREL_ROOT}/app-data/<app>/exports.sh`. That is still true on umbrelOS 2.0
(`legacy-compat/app-script`, `source_app`).

On every app start the one-shot `migrate` service runs first. The first time
(`"migrated"` not yet in `data/config.json`), it reads that `.env` read-only
and creates **runner #1** with the 1.x values: name `rozsa-umbrel`, org
`ORG_NAME`, labels `self-hosted,linux,x64,umbrel`, group `default`, not
ephemeral. Then it sets `"migrated": true`, so the import never runs again. The
runner registers with `--replace`, so it takes over the existing GitHub
registration and workflows keep working. `migrate` always exits 0. A failed
import leaves you with no runner #1 (add it in the UI, or roll back) but
doesn't block the app.

2.x doesn't use `exports.sh` or compose interpolation for secrets, so the store
doesn't ship an `exports.sh`. **The migration never changes or deletes `.env`
or `exports.sh`.** They stay in place so a rollback to 1.x works. While `.env`
exists, the UI shows a reminder that the token is stored twice. Once the
post-upgrade checks pass and you no longer need a rollback, delete them:

```sh
sudo rm /home/umbrel/umbrel/app-data/csikosjanos-github-runner/{.env,exports.sh}
```

A fresh install starts with no runners; add them in the UI.

### Upgrade checks (the runner is production)

Before upgrading:

1. `gh api orgs/<ORG>/actions/runners --jq '.runners[] | {name,status,busy}'`
   shows `rozsa-umbrel` as `online`.
2. `sudo docker logs --tail 20 csikosjanos-github-runner_runner_1` ends with `Listening for Jobs`.
3. No job is running on it (`busy: false`). The upgrade stops the runner and
   would cancel a running job.

After upgrading (Umbrel dashboard → Update):

1. The app UI shows `rozsa-umbrel` as **idle**.
2. The same `gh api` call shows it `online` (one runner, not a duplicate).
3. `sudo docker logs csikosjanos-github-runner_runner_1` shows
   `[runner 1] ... Listening for Jobs`.
4. Run one real job, for example a `workflow_dispatch` workflow in an org repo:
   ```yaml
   on: workflow_dispatch
   jobs:
     smoke:
       runs-on: [self-hosted, umbrel]
       steps:
         - uses: actions/checkout@v4
         - run: uname -a && id && git --version
   ```
   The card shows `busy: smoke` and then goes back to idle. The job succeeds.
   Jobs now run as an unprivileged user. If a real workflow needs `sudo`,
   that's a reason to roll back.

### Rollback to 1.x

`.env` and `exports.sh` are untouched, so 1.x works again as soon as its
compose file is back. umbreld offers an update whenever the store's version
string differs (`!==`), so:

- **Through the store (preferred):** in `csikosjanos/umbrel-app-store`, revert
  this change but set `version: "2.0.1"` (any new string) in
  `umbrel-app.yml`. Then use Update in the dashboard. 1.x registers again with
  `--replace` under the same name.
- **Immediately, on the box:** put the 1.x `docker-compose.yml` (from git
  history) into `app-data/csikosjanos-github-runner/` and restart the app from
  the dashboard. The next store update overwrites it again.

2.x's `data/` dir is left behind and ignored by 1.x.

### Failure isolation

- A failed import only means runner #1 is missing. It never stops the app.
- Each runner has its own supervising thread. A runner crash restarts only
  that runner, with backoff (5 s up to 5 min). If it fails quickly, it
  registers again.
- An HTTP or UI error affects only that request. If the HTTP server loop
  itself dies, it restarts without touching the runners.
- If the manager process itself exits, Docker restarts the container
  (`unless-stopped`) and every runner registers again (about 1 min). In this
  design the runners are its child processes, so this case can't be avoided.

## Files

| Path | What |
|---|---|
| `docker-compose.yml` | one-shot `migrate`, `runner` (manager + runners), app_proxy → `:8080` |
| `manager/manager.py` | Supervisor + JSON API (`/api/runners`, `…/<id>`, `…/<id>/{start,stop,restart,logs}`) |
| `manager/index.html` | The UI (vanilla JS) |
| `manager/test_manager.py` | Tests with a fake runner binary and a fake GitHub API: `python3 -m unittest -v test_manager.py` |
| `manager/Dockerfile` | `FROM myoung34/github-runner:<pinned>` + the two files above |

To release: change the code, bump `version` in `umbrel-app.yml` **and** the
image tag in `docker-compose.yml`. The workflow builds and pushes
`ghcr.io/csikosjanos/github-runner-manager:<version>`.

## Logs

```sh
sudo docker logs -f csikosjanos-github-runner_runner_1   # all runners, prefixed [runner <id>]
```

## Limitations

- Linux jobs only (x64 or ARM64, depending on the box). Windows/macOS builds need their own runners.
- Jobs run as an unprivileged user without sudo, so `apt-get install` in a job
  won't work. Use `setup-*` actions or tools that are already in the image.
- No `docker.sock`, so jobs can't build images.
- Runner auto-update is disabled (`--disableupdate`). Runner versions change
  with the base image tag in `manager/Dockerfile`.
