# midichlorian Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A playbook-run coordinator for the Jedi: an HTTP service (Cere primary, mace for Cere), a web UI at
`midichlorian.home.nakomis.com`, and a `midichlorian` MCP, all written and tested ahead of Cere's arrival so that
deploying it is just running playbooks.

**Architecture:** One uv project in `mcps/midichlorian` with two entry points. `midichlorian-server` (FastAPI,
SQLite, asyncio subprocesses) owns per-host queues, gates, playbook runs and verification. `midichlorian-mcp`
(FastMCP, stdio, launched by meta-mcp on the Mac) mirrors the API and pushes results into the requesting session
over its messaging socket. Infrastructure (Roles Anywhere deploy role, SSM moves, Cere/mace/Leia Ansible) lives in
`nakomis/home-infra`.

**Tech Stack:** Python 3.13, uv/hatchling, FastAPI + uvicorn, sse-starlette, httpx, `mcp[cli]<2` (FastMCP),
pytest + pytest-asyncio; Ansible; AWS CDK (TypeScript); IAM Roles Anywhere (`aws_signing_helper` 1.7.0).

**Spec:** `docs/superpowers/specs/2026-10-19-midichlorian-design.md` (this repo).

## Global Constraints

- British English in all prose, comments, UI text and commit messages. No `Co-Authored-By` or "Generated with" lines.
- Plane ref HOME-510 in branch names and PR titles: mcps branch `home-510-midichlorian`; home-infra branch `home-510-midichlorian-infra`.
- Hosts: `luke`, `leia`, `cal`, `mace`, `rey`, `cere`. Cere instance serves `luke leia cal mace rey`; mace instance serves `cere`. No host runs its own playbook.
- Server port **9140** on both instances. API token: SSM `/midichlorian/api-token` (64 lowercase hex). GitHub token: SSM `/midichlorian/github-token`.
- Only commits on `origin/main` of `nakomis/home-infra` are deployable.
- Ro-root hosts: `luke leia cal mace`. Printer-sensitive: `rey`, `leia`, and Luke runs that are full or include tag `samba`.
- Cthulhu idle labels (from `rey/ansible/playbook.yml:1714`): `'' Idle Complete Stopped Cancelled`. OctoPrint busy states: `Printing Pausing Paused Resuming Cancelling Finishing`.
- `aws_signing_helper` **1.7.0 only** (1.8.0+ returns 403). Checksums from `cal/ansible/playbook.yml:295-306` and `scripts/collect-cert.sh:79-89` in home-infra.
- Deployed marker on each host: `/var/log/midichlorian/deployed` (one line: `<sha> <iso-time> <tags-or-full>`).
- Socket callback: line 1 `{"type":"auth","token":"<CLAUDE_CODE_MESSAGING_TOKEN>"}`, line 2 `{"type":"user","message":{"role":"user","content":"<text>"}}` (undocumented; isolated in `notify.py`). Never log the token.
- home-infra Ansible changes obey "EC2 stand-in decisions" (`# dr:` comment or `dr_ec2` gate on every created/changed task; `scripts/dr-check.py` 0 MISSING).
- Nothing live is changed by this plan's tasks except where a task says "LIVE — needs Martin's OK".

## Review Focus

1. A `deploy` for a SHA that is on main but **newer than the runner's local clone** — the server must `git fetch` before the ancestor/on-main checks, not reject it.
2. Two requests for the same host arriving while one run is **running** (not queued) — the second must queue behind it, never start in parallel and never merge into the running job.
3. Cthulhu or OctoPrint **unreachable** (printer off, container stopped) — treated as idle with a note, not as an indefinite hold.
4. A multi-host request whose **first host fails** — later hosts are marked `skipped`, every requester is told, and nothing later in that request runs.
5. Server restart with a job in `running` — it becomes `interrupted` on start-up and the queue continues with the next job.

---

## File Structure

### mcps (`midichlorian/`)

| File | Responsibility |
|---|---|
| `pyproject.toml` | uv project, deps, entry points `midichlorian-server`, `midichlorian-mcp` |
| `src/midichlorian/config.py` | `Settings` from env: instance name, served hosts, data dir, repo dir, token, peers |
| `src/midichlorian/hosts.py` | `HostSpec` registry (ssh target, wrapper script, ro_root, linux, printer rules) |
| `src/midichlorian/store.py` | SQLite: requests, jobs, deployed state; restart recovery |
| `src/midichlorian/gitops.py` | fetch, `is_on_main`, `is_ancestor`, reset checkout to a commit |
| `src/midichlorian/scheduler.py` | pure merge/ordering rules: which queued job a request joins, which job runs next |
| `src/midichlorian/printers.py` | Cthulhu and OctoPrint probes → `PrinterState` |
| `src/midichlorian/gates.py` | AWS, printer hold, Luke backups, Docker checks |
| `src/midichlorian/recap.py` | parse PLAY RECAP and failing task from a log |
| `src/midichlorian/remote.py` | `run_ssh(host, cmd)` helper (async subprocess) |
| `src/midichlorian/verify.py` | post-run checks (ro root, failed units, containers, stale CIFS) |
| `src/midichlorian/runner.py` | `PlaybookRunner` protocol, `AnsibleRunner`, log writing |
| `src/midichlorian/worker.py` | per-host worker loop: gates → run → verify → record → mark done |
| `src/midichlorian/api.py` | FastAPI app: endpoints, SSE log follow, peer aggregation, static UI |
| `src/midichlorian/web/{index.html,app.js,style.css}` | the UI |
| `src/midichlorian/notify.py` | messaging-socket delivery |
| `src/midichlorian/mcp_server.py` | FastMCP tools + job follower |
| `tests/…` | one test module per source module |
| `README.md` | what it is, config, running locally |

### home-infra

| File | Responsibility |
|---|---|
| `infra/cdk/lib/midichlorian-stack.ts` (+ wire in `bin/infra.ts`, test) | Roles Anywhere profile + `midichlorian-deploy` role (CN `cere`, `mace`) with the playbooks' AWS permissions |
| `{luke,leia,cal,mace,rey}/ansible/playbook.yml`, `scripts/build-leia-client-ca-bundle.sh` | nakotp legacy files from prod SSM instead of `mh` S3 |
| `cere/ansible/tasks/midichlorian.yml`, `cere/ansible/templates/com.nakomis.midichlorian.plist.j2`, `cere/ansible/playbook.yml` | Cere instance (LaunchAgent) |
| `mace/deployment/midichlorian/{Dockerfile,docker-compose.yml}`, `mace/ansible/playbook.yml` | mace instance (container) |
| `shared/midichlorian/aws-config.j2`, `shared/midichlorian/ssh-config` | runner AWS profile + SSH config, used by both |
| `leia/deployment/nginx/midichlorian.home.nakomis.com.conf`, `leia/ansible/playbook.yml` | vhost |
| `ssh-keys/midichlorian-cere.pub`, `ssh-keys/midichlorian-mace.pub` | runner SSH identities authorised on the Jedi |
| `docs/runbooks/midichlorian.md`, `cere/CLAUDE.md`, `mace/CLAUDE.md`, `leia/CLAUDE.md` | docs |

---

## Part A — the midichlorian package (mcps)

### Task A1: Scaffold, settings and host registry

**Files:** Create `midichlorian/pyproject.toml`, `src/midichlorian/{__init__,config,hosts}.py`, `tests/test_config.py`, `tests/test_hosts.py`, `README.md`.

**Interfaces — Produces:**
- `hosts.HostSpec(name: str, ssh: str, wrapper: str, ro_root: bool, linux: bool, docker: bool, printer_sensitive: Literal["always","samba","never"], needs_backup_gate: bool)`
- `hosts.HOSTS: dict[str, HostSpec]`; `hosts.printer_hold_applies(host: str, tags: list[str] | None) -> bool`
- `config.Settings(instance: str, served: frozenset[str], data_dir: Path, repo_dir: Path, repo_url: str, token: str, peers: dict[str, str], bind: str, port: int)`; `config.load_settings(env: Mapping[str,str] = os.environ) -> Settings` (env `MIDICHLORIAN_INSTANCE`, `_SERVED` comma list, `_DATA_DIR`, `_REPO_DIR`, `_REPO_URL` default `https://github.com/nakomis/home-infra.git`, `_TOKEN`, `_PEERS` as `name=url,…`, `_BIND` default `0.0.0.0`, `_PORT` default 9140). Refuses `served` containing `instance`.

Host table (ssh targets match the inventories): luke `luke.local` ro linux docker samba backup; leia `leia.local` ro linux docker always; cal `cal.local` ro linux docker never; mace `mace.local` ro linux docker never; rey `rey.local` rw linux docker always; cere `cere.local` rw macOS no-docker never. Wrapper `scripts/<host>-ansible.sh`.

- [ ] Write tests: `printer_hold_applies("rey", None) is True`; `("luke", None)` True (full run); `("luke", ["claude-chats"])` False; `("luke", ["samba"])` True; `("cal", None)` False; `load_settings` rejects `MIDICHLORIAN_INSTANCE=cere` with `MIDICHLORIAN_SERVED=cere`; parses peers.
- [ ] Run `uv run pytest -q` → fails (no module).
- [ ] Implement; pyproject deps: `fastapi>=0.115`, `uvicorn>=0.30`, `sse-starlette>=2`, `httpx>=0.27`, `mcp[cli]<2`; dev group `pytest>=8`, `pytest-asyncio>=0.23`. Scripts: `midichlorian-server = "midichlorian.api:main"`, `midichlorian-mcp = "midichlorian.mcp_server:main"`.
- [ ] Tests pass. Commit `feat(midichlorian): scaffold, settings and host registry (HOME-510)`.

### Task A2: Store (SQLite)

**Files:** `src/midichlorian/store.py`, `tests/test_store.py`.

**Interfaces — Produces:** dataclass `Job(id: int, request_id: int, host: str, sha: str, tags: list[str] | None, state: str, position: int, created: str, started: str|None, finished: str|None, result: str|None, detail: dict, log_path: str|None, requesters: list[str], override_printer: bool)`; states `queued waiting running succeeded failed skipped cancelled interrupted already`.
`Store(path)` methods: `create_request(hosts, sha, tags, override_printer, requester) -> int`; `add_job(request_id, host, sha, tags, position, override_printer, requester) -> Job`; `merge_requester(job_id, requester, sha, tags)`; `get_job(id)`; `jobs(host=None, states=None, limit=100) -> list[Job]`; `set_state(job_id, state, **fields)`; `request_jobs(request_id) -> list[Job]`; `record_deploy(host, sha, tags, at)`; `deployed(host) -> dict | None` (last full + last per tag set); `recover_interrupted() -> list[int]`.

- [ ] Tests: round-trip a job; `merge_requester` appends requester and widens tags (`None` = full wins); `recover_interrupted` flips `running`→`interrupted` and returns ids; `deployed` returns latest full deploy.
- [ ] Implement with `sqlite3` (WAL, `check_same_thread=False`, one lock). Tables `requests`, `jobs`, `deploys`. JSON columns for tags/detail/requesters.
- [ ] Pass. Commit `feat(midichlorian): SQLite store (HOME-510)`.

### Task A3: gitops

**Files:** `src/midichlorian/gitops.py`, `tests/test_gitops.py`.

**Interfaces — Produces (async):** `fetch(repo: Path)`; `ensure_clone(repo: Path, url: str)`; `is_on_main(repo, sha) -> bool` (`git merge-base --is-ancestor <sha> origin/main`); `is_ancestor(repo, a, b) -> bool`; `resolve(repo, ref) -> str` (full sha); `checkout(repo, sha)` (`git reset --hard <sha>` + `git clean -fdx -e .venv`); `main_head(repo) -> str`.

- [ ] Tests build a throwaway repo pair (bare "origin" + clone) in `tmp_path`: commit A on main, push; commit B on a side branch. Assert `is_on_main(A)` True, `is_on_main(B)` False; a commit pushed to origin **after** clone is not on main until `fetch` (Review Focus 1); `checkout` leaves the worktree at the sha.
- [ ] Implement with `asyncio.create_subprocess_exec("git", "-C", …)`; raise `GitError(stderr)` on non-zero.
- [ ] Pass. Commit `feat(midichlorian): git helpers (HOME-510)`.

### Task A4: Scheduler rules (pure)

**Files:** `src/midichlorian/scheduler.py`, `tests/test_scheduler.py`.

**Interfaces — Produces:**
- `covers(done_sha_is_descendant: bool, done_tags: list[str]|None, want_tags: list[str]|None) -> bool` — a full run covers any tags; tagged covers tagged only if `set(want) <= set(done)`.
- `merge_target(queued: list[Job], host: str, want_tags) -> Job | None` — only jobs in state `queued` or `waiting` for that host (never `running`, Review Focus 2); the merged job keeps the union of tags, `None` if either is full.
- `next_runnable(jobs: list[Job]) -> list[Job]` — per host at most one; a job with `position > 0` runs only when every earlier job of its request is `succeeded`/`already`; if an earlier one is `failed/cancelled/interrupted/skipped`, return it in a separate `to_skip` list (Review Focus 4). Signature `next_runnable(jobs) -> tuple[list[Job], list[Job]]` (`runnable`, `to_skip`).

- [ ] Tests: merge into queued not running; full absorbs tags; order luke→leia→mace runs leia only after luke succeeded; luke failed ⇒ leia, mace in `to_skip`; two hosts independent run concurrently.
- [ ] Implement. Pass. Commit `feat(midichlorian): queue merge and ordering rules (HOME-510)`.

### Task A5: Printers and gates

**Files:** `src/midichlorian/printers.py`, `src/midichlorian/gates.py`, `src/midichlorian/remote.py`, `tests/test_printers.py`, `tests/test_gates.py`.

**Interfaces — Produces:**
- `remote.run(argv: list[str], timeout: float = 60, env: dict|None = None) -> RunResult(rc, stdout, stderr)`; `remote.ssh(host: HostSpec, command: str, timeout=60) -> RunResult` (`ssh -o BatchMode=yes -o ConnectTimeout=10 nakomis@<ssh> <command>`).
- `printers.PrinterState(name: str, busy: bool, detail: str, reachable: bool)`.
- `printers.cthulhu_state(client: httpx.AsyncClient, url="http://172.29.0.36:9120/api/status") -> PrinterState` — unreachable ⇒ `busy=False, reachable=False` (Review Focus 3); `connected` false ⇒ idle; else busy iff `print.statusLabel` not in the idle set.
- `printers.octoprint_state(ssh, api_key: str) -> PrinterState` — on Leia: `docker inspect -f '{{.State.Running}}' gateway-octoprint-1` (not running ⇒ idle, unreachable=False); otherwise send the key on ssh **stdin** (never argv) to `read -r OCTO_KEY; export OCTO_KEY; docker exec -e OCTO_KEY gateway-octoprint-1 sh -c 'curl -s -H "X-Api-Key: $OCTO_KEY" http://localhost:5000/api/job'`. Busy iff `state` in the busy set. The server reads the key from SSM `/octoprint/api-key` once at start-up.
- `gates.GateResult(ok: bool, wait: bool, reason: str)`.
- `gates.check_aws(env) -> GateResult` (`aws sts get-caller-identity --profile nakom.is-admin`).
- `gates.check_printers(host, tags, override, probes) -> GateResult` — `wait=True` with reason naming the busy printer unless `override`.
- `gates.run_luke_backups(ssh) -> GateResult` (`./run-backups.sh`, rc 0 and output contains `All databases backed up and verified`).
- `gates.check_docker() -> GateResult` (`docker info`), applied for `rey` and `luke`.

- [ ] Tests with `httpx.MockTransport` and a fake ssh runner: Cthulhu `Lifting` ⇒ busy; `Complete` ⇒ idle; connection error ⇒ idle+unreachable; OctoPrint container stopped ⇒ idle; `Printing` ⇒ busy; `check_printers("rey", None, override=False)` waits; `override=True` passes; `("cal", …)` never consults probes; Luke `["claude-chats"]` not held.
- [ ] Implement. Pass. Commit `feat(midichlorian): printer probes and gates (HOME-510)`.

### Task A6: Recap parsing and verification

**Files:** `src/midichlorian/recap.py`, `src/midichlorian/verify.py`, `tests/test_recap.py`, `tests/test_verify.py`, `tests/fixtures/*.log`.

**Interfaces — Produces:**
- `recap.parse(log_text: str) -> Recap(ok: int, changed: int, unreachable: int, failed: int, skipped: int, failing_task: str|None)`; `None` counts if no recap. `failing_task` = the last `TASK [...]` before the first `fatal:` / `UNREACHABLE!`.
- `verify.Check(name: str, ok: bool, detail: str)`; `verify.run_checks(host: HostSpec, before: dict, ssh) -> list[Check]`: ro root (`findmnt -no OPTIONS /` first field `ro`, ro hosts only), failed units (`systemctl --failed --no-legend` empty, linux), containers (`docker ps -a --format '{{.Names}}|{{.State}}|…'` + `docker inspect` restart counts: fail if a container that was running before is not running, or its RestartCount rose by >1), Samba restart (Luke only: `docker inspect -f '{{.State.StartedAt}}' samba` changed vs `before`) ⇒ then on rey/leia/mace `timeout 10 ls /mnt/print-recordings` and, if it fails with "Stale file handle", `sudo umount -l -t cifs /mnt/print-recordings` (skipped on rey while printing; the check reports it).
- `verify.snapshot(host, ssh) -> dict` — container states/restarts and Samba StartedAt, taken before the run.

- [ ] Fixtures: the real recaps from 19 Oct (`ok=271 … unreachable=1` with the `Restore / to read-only` failure; a clean `ok=273 failed=0`). Tests: parse both; `failing_task == "Restore / to read-only — OS-layer"`; `run_checks` with a fake ssh returning `rw` fails the ro check; a container restart-count jump fails; Samba StartedAt change triggers the CIFS step.
- [ ] Implement. Pass. Commit `feat(midichlorian): recap parser and post-run checks (HOME-510)`.

### Task A7: Runner and worker

**Files:** `src/midichlorian/runner.py`, `src/midichlorian/worker.py`, `tests/test_worker.py`.

**Interfaces — Produces:**
- `runner.PlaybookRunner` protocol: `async run(host: HostSpec, tags: list[str]|None, repo: Path, log: Path, env: dict) -> int`.
- `runner.AnsibleRunner` — `scripts/<host>-ansible.sh [--tags a,b]` with cwd `repo`, env `DEPLOY_SKIP_CHECKOUT_CHECK=1`, `ANSIBLE_FORCE_COLOR=0`, `GH_TOKEN`, PATH incl. `/opt/homebrew/bin`; stdout+stderr appended line by line to `log`; returns rc.
- `worker.Coordinator(settings, store, runner, probes, ssh, clock)`: `submit(hosts, sha, tags, override_printer, requester) -> SubmitResult(request_id, jobs: list[{host, job_id|None, state}])`; `async tick()` — one scheduling pass (testable); `async run_forever()`; `cancel(job_id)` (queued/waiting only); emits events to `subscribers` (asyncio.Queue per listener) for SSE and for MCP followers.
- Flow per job: fetch → refuse if not on main (`failed`, reason) → already-deployed check (`store.deployed` + `gitops.is_ancestor`) ⇒ `already` → gates (AWS fail ⇒ failed; printer wait ⇒ `waiting`, re-checked each tick, 60 s) → Luke backups → `snapshot` → `checkout(origin/main head)` (deploy main's head, which contains the sha) → run → `recap.parse` → `verify.run_checks` → success iff rc==0, recap failed==0 and unreachable==0, all checks ok → `ssh host 'sudo mkdir -p /var/log/midichlorian && echo … | sudo tee /var/log/midichlorian/deployed'` + `store.record_deploy` → state + detail (`recap`, `checks`, `failing_task`, `log_tail` last 40 lines on failure).
- Start-up: `store.recover_interrupted()`.

- [ ] Tests with a `FakeRunner` (writes a fixture log, returns rc) and fakes for gates/ssh/git: success path records deploy; failing recap ⇒ `failed` + log tail + later hosts `skipped` (Review Focus 4); printer busy ⇒ `waiting`, then idle ⇒ runs; second request for a running host queues (Review Focus 2); `already` short-circuit; `recover_interrupted` on construct then next job runs (Review Focus 5); not-on-main refused.
- [ ] Implement. Pass. Commit `feat(midichlorian): runner and coordinator (HOME-510)`.

### Task A8: HTTP API

**Files:** `src/midichlorian/api.py`, `tests/test_api.py`.

**Interfaces — Produces:** `create_app(settings, coordinator, peers_client) -> FastAPI`; `main()` (uvicorn on `settings.bind:port`).
Endpoints exactly as the spec table plus `GET /api/info` → `{instance, served, version}` and `GET /healthz` (no auth). Auth: `Authorization: Bearer <token>` **or** `X-Midichlorian-Token` header (Leia injects the latter); 401 otherwise. `POST /api/deploy` body `{hosts: list[str], sha: str, tags?: list[str], override_printer?: bool, requester: str}`; 400 for hosts not served here (message names the serving instance from peers' `/api/info`). GET list endpoints on an instance with peers merge peer results (`instance` field on each item; peer failure ⇒ partial result + `peers_unreachable`). `GET /api/runs/{id}/log?follow=1` → `EventSourceResponse` streaming appended lines until the job finishes; `?since=N` → `{text, next}`. Static UI at `/`.

- [ ] Tests (FastAPI `TestClient`, fake coordinator): 401 without token; deploy for unserved host 400; hosts merge with a mocked peer; log paging; SSE emits lines then closes on finish.
- [ ] Implement. Pass. Commit `feat(midichlorian): HTTP API (HOME-510)`.

### Task A9: Web UI

**Files:** `src/midichlorian/web/index.html`, `app.js`, `style.css`; `tests/test_web.py` (served, contains the expected element ids).

Single page, no build step, light/dark via `prefers-color-scheme`. Sections: **Jedi** cards (from `/api/hosts`: deployed sha (short, linked to GitHub commit), time, last result badge, gate badges, running indicator); **Queue** (`/api/queue`, cancel button on queued/waiting); **History** (`/api/runs?limit=50`, row click opens detail: checks list, recap, requesters); **Live log** panel (`EventSource('/api/runs/<id>/log?follow=1')`, auto-scroll, opens for running jobs). Poll hosts/queue/history every 5 s. Browser calls carry no token (Leia adds it).

- [ ] Test the page is served and references `app.js`; manual check later in a browser.
- [ ] Implement. Commit `feat(midichlorian): web UI (HOME-510)`.

### Task A10: MCP and socket callback

**Files:** `src/midichlorian/notify.py`, `src/midichlorian/mcp_server.py`, `tests/test_notify.py`, `tests/test_mcp.py`.

**Interfaces — Produces:**
- `notify.deliver(text: str, socket_path: str | None = None, token: str | None = None) -> bool` — reads `CLAUDE_CODE_MESSAGING_SOCKET`/`_TOKEN` from env by default; writes the two JSON lines; returns False (and logs without the token) if absent or the connect fails.
- MCP tools (FastMCP `midichlorian`): `hosts()`, `queue()`, `runs(host: str | None = None, limit: int = 20)`, `run(run_id: int)`, `run_log(run_id: int, since: int = 0)`, `deploy(hosts: list[str], sha: str = "main", tags: list[str] | None = None, override_printer: bool = False, requester: str = "")`, `cancel(run_id: int)`. Config env: `MIDICHLORIAN_INSTANCES` (`cere=http://<ip>:9140,mace=http://172.29.0.3:9140`), `MIDICHLORIAN_TOKEN`. Routing: build host→instance map from each instance's `/api/info`; a multi-host deploy spanning instances is split per instance, preserving order (later group submitted only after the earlier group succeeds — followed by the MCP). `sha="main"` resolves to origin/main head server-side.
- Follower: after `deploy`, a background task polls `GET /api/runs/{id}` every 10 s per job; on terminal state (or first `waiting`) calls `notify.deliver` with a one-paragraph summary: host, sha[:8], result, recap counts, failing task, log tail (≤20 lines), UI link `https://midichlorian.home.nakomis.com/#run-<id>`.

- [ ] Tests: `deliver` against a temporary Unix socket server asserts the exact two lines and that the token never appears in log output; `deploy` routing splits hosts across two mocked instances; follower delivers once on `succeeded`.
- [ ] Implement. Pass. Commit `feat(midichlorian): MCP tools and session callbacks (HOME-510)`.

### Task A11: README and full-suite check

- [ ] README: purpose, the two entry points, env vars for each, running locally (`MIDICHLORIAN_INSTANCE=dev … uv run midichlorian-server`), meta-mcp entry example, safety rules (override only on Martin's word).
- [ ] `uv run pytest -q` all green; `uv run ruff check` if configured in repo (skip otherwise).
- [ ] Commit; push; open PR `feat(midichlorian): playbook-run coordinator for the Jedi (HOME-510)`.

---

## Part B — infrastructure (home-infra, branch `home-510-midichlorian-infra`)

### Task B1: Deploy role (CDK)

**Files:** Create `infra/cdk/lib/midichlorian-stack.ts`, `infra/cdk/test/midichlorian-stack.test.ts`; modify `infra/cdk/bin/infra.ts`.

Copy the `naktv-devmode-stack.ts` pattern: read anchor ARN from SSM `/conversation-memory/<env>/trust-anchor-arn`; `CfnProfile` `midichlorian-deploy`; role `midichlorian-deploy-<env>` trusted by `rolesanywhere.amazonaws.com` with `aws:SourceArn` = anchor and `aws:PrincipalTag/x509Subject/CN` in `["cere","mace"]` (`StringEquals` list), plus `sts:TagSession`/`sts:SetSourceIdentity`; max session 1 h. Policy (from `research-aws-calls.md`):
- `ssm:GetParameter`, `ssm:GetParameters`, `ssm:GetParametersByPath` on the listed prefixes (`/datadog/*`, `/grafana-cloud/*`, `/new-relic/*`, `/honeycomb/*`, `/vmauth/*`, `/gateway/certbot/*`, `/taiga/backup/*`, `/pushover/*`, `/scrutiny/notify/*`, `/samba/*`, `/cthulhu/*`, `/plane/*`, `/atc/*`, `/octoprint/*`, `/nexus/*`, `/bau/*`, `/nakotp/*`, `/guacamole/*`, `/github-runner/*`, `/hosts/*`, `/nakomis-infra/prod/cognito/*`, `/home-certs/prod/*`, `/nakom-admin/cognito/*`, `/nakomis-shell/prod/cognito/*`, `/conversation-memory/prod*`, `/shortener-hits/prod/*`, `/projections/prod*`, `/naktv/prod*`, `/cv-chat/prod*`, `/victoriametrics-backup/prod*`, `/nakom.is/shortener-hits*`, `/midichlorian/*`).
- `ssm:PutParameter` on exactly `/octoprint/admin-password`, `/octoprint/admin-password-hash`, `/conversation-memory/prod/age-identity`, `/conversation-memory/prod/age-recipient`.
- `kms:Decrypt` on `alias/aws/ssm` key (condition `kms:ViaService = ssm.eu-west-2.amazonaws.com`).
- `ecr:GetAuthorizationToken` on `*`; `s3:GetObject` on `arn:aws:s3:::home-ca-prod/ca/cert.pem`; `sts:GetCallerIdentity` implicit.
Outputs written to SSM: `/midichlorian/<env>/role-arn`, `/midichlorian/<env>/profile-arn` (trust anchor already at `/conversation-memory/<env>/trust-anchor-arn`).

- [ ] Test (jest/assertions like the other stack tests): trust condition lists exactly `cere`,`mace`; PutParameter resources are exactly the four; no `s3:*`/`ssm:*` wildcards on actions.
- [ ] `pnpm test` green; `cdk synth` for prod. Commit `feat(infra): midichlorian deploy role via Roles Anywhere (HOME-510)`. Deploys through CI on merge.

### Task B2: nakotp legacy files from prod SSM

**Files:** `{luke,leia,cal,rey}/ansible/playbook.yml` (client.p12 + ca.crt fetch tasks), `mace/ansible/playbook.yml:617` (ca.crt), `scripts/build-leia-client-ca-bundle.sh`.

- LIVE — needs Martin's OK (step 1): create SSM `/nakotp/legacy/client-p12-b64` (SecureString, base64 of `client.p12`) and `/nakotp/legacy/ca-crt` (String) in prod from `s3://nak-sandbox-certs/{client.p12,ca.crt}` (profile `mh` to read, `nakom.is-admin` to write); never print values.
- Replace each `aws s3 cp s3://{{ nakotp_certs_s3_bucket }}/client.p12 /tmp/nakotp-client.p12 --profile mh` with `aws ssm get-parameter --name /nakotp/legacy/client-p12-b64 --with-decryption --query Parameter.Value --output text --profile nakom.is-admin --region eu-west-2 | base64 -d > /tmp/nakotp-client.p12` (shell task, `no_log`, `umask 077`), likewise `ca.crt`. Remove `nakotp_certs_s3_bucket`/`nakotp_certs_aws_profile` vars. `build-leia-client-ca-bundle.sh`: read the nakotp CA from SSM.
- `# dr:` comment on every changed task (`same — the stand-in reads the same parameters`).
- [ ] dr-check 0 MISSING; `--list-tasks` for all five. Verify on phi with a `--check` run of one host after step 1 (LIVE, with OK). Commit `refactor(ansible): nakotp legacy files from prod SSM, not the mh account (HOME-510)`.

### Task B3: Runner SSH identities

- LIVE — needs Martin's OK: generate two ed25519 keys on phi (`midichlorian-cere`, `midichlorian-mace`), store private keys in SSM `/midichlorian/ssh/{cere,mace}` (SecureString), commit public keys as `ssh-keys/midichlorian-cere.pub`, `ssh-keys/midichlorian-mace.pub`. Every playbook already authorises `ssh-keys/*.pub`, so the next run of each Jedi admits them (Cere's key must reach luke, leia, cal, mace, rey; mace's key must reach cere).
- `shared/midichlorian/ssh-config`: `Host *.local` → `User nakomis`, `IdentityFile ~/.ssh/midichlorian`, `StrictHostKeyChecking accept-new`, `ServerAliveInterval 30`.
- [ ] Commit with B4.

### Task B4: Cere instance

**Files:** `cere/ansible/tasks/midichlorian.yml`, `cere/ansible/templates/com.nakomis.midichlorian.plist.j2`, `cere/ansible/templates/midichlorian.env.j2`, `shared/midichlorian/aws-config.j2`; modify `cere/ansible/playbook.yml` (import with `tags: [midichlorian]`, `# dr: n/a — macOS-only (launchd, brew); the stand-in is Ubuntu`), `cere_brew_formulae` += `ansible`, `flock`, `awscli`, `gh`, `age` (check which are already there).

Tasks: dirs `~/midichlorian/{src,repo,data,logs}`, `~/.config/midichlorian` 0700; install `aws_signing_helper` 1.7.0 Darwin aarch64 to `~/.local/bin` (get_url + checksum); copy Cere's cert pair from SSM `/hosts/cere/mtls/{client-cert,client-key}` (controller, `no_log`) to `~/.config/midichlorian/cere.{crt,key}.pem` 0600; read `/midichlorian/<env>/{role-arn,profile-arn}` and anchor; template `~/.aws/config` profile `nakom.is-admin` with `credential_process = ~/.local/bin/aws_signing_helper credential-process --certificate … --private-key … --trust-anchor-arn … --profile-arn … --role-arn …` and `region = eu-west-2` (**only if no SSO profile already exists there** — Cere's own CI may use SSO; else write `~/.config/midichlorian/aws-config` and set `AWS_CONFIG_FILE` in the env file instead — prefer this to avoid clashing); SSH key from SSM `/midichlorian/ssh/cere` to `~/.ssh/midichlorian` 0600 + `~/.ssh/config` include of the shared config; git checkout of `nakomis/mcps` to `~/midichlorian/src` (version main) and `uv sync --frozen` in `midichlorian/`; clone home-infra to `~/midichlorian/repo`; `ansible-galaxy collection install -r` for each host's requirements; env file (0600) with `MIDICHLORIAN_INSTANCE=cere`, `_SERVED=luke,leia,cal,mace,rey`, `_TOKEN` (SSM `/midichlorian/api-token`), `_PEERS=mace=http://172.29.0.3:9140`, `GH_TOKEN` (SSM `/midichlorian/github-token`), `AWS_CONFIG_FILE`, `DOCKER_HOST=unix://~/.colima/default/docker.sock`, PATH; LaunchAgent `com.nakomis.midichlorian` (OTel idiom: bootout/bootstrap/kickstart handler) running `uv run --project ~/midichlorian/src/midichlorian midichlorian-server`; health wait `http://127.0.0.1:9140/healthz`.
Note in `cere/CLAUDE.md`: the first launch from launchd needs the macOS Local Network permission dialog clicked on Cere's screen.

- [ ] dr-check 0 MISSING; `ansible-playbook --syntax-check` and `--list-tasks --tags midichlorian`. Commit `feat(cere): midichlorian runner as a LaunchAgent (HOME-510)`.

### Task B5: mace instance

**Files:** `mace/deployment/midichlorian/Dockerfile`, `mace/deployment/midichlorian/docker-compose.yml`; modify `mace/ansible/playbook.yml` (block `tags: [midichlorian]`, `when: not dr_ec2`, screeneye-style build from the controller's mcps checkout).

Image: `docker.home.nakomis.com/library/python:3.13-slim` base, `apt-get install openssh-client git curl unzip ca-certificates`, AWS CLI v2 (x86_64 installer, checksum-pinned), `aws_signing_helper` 1.7.0 Linux x86_64 (checksum from `cal/ansible/playbook.yml:295-306`), `gh` (Debian package from GitHub's apt repo), `pip install ansible-core==2.19.*` + collections from `cere/ansible/requirements.yml`, `uv` + the midichlorian package. Compose: bridge network with `ports: "172.29.0.3:9140:9140"`, volumes `/mnt/data/midichlorian/{data,repo}`, `/mnt/data/midichlorian/secrets` (ro) holding `mace.{crt,key}.pem`, `ssh_key`, `aws-config`; env from `.env` (0600, rendered from SSM: token, GH token, peers empty, served `cere`, instance `mace`). `flock` is in util-linux (Debian base has it). Health wait `http://172.29.0.3:9140/healthz`. Handler `Restart midichlorian`. `# dr: n/a — the stand-in does not coordinate deploys` on the block.

- [ ] dr-check, `--list-tasks --tags midichlorian`, `docker build` locally on phi (`--platform linux/amd64`). Commit `feat(mace): midichlorian runner for Cere's playbook (HOME-510)`.

### Task B6: Leia vhost

**Files:** `leia/deployment/nginx/midichlorian.home.nakomis.com.conf` (templated); modify `leia/ansible/playbook.yml` (`nginx_vhosts` + `nginx_templated_vhosts`, var `midichlorian_upstream: "{{ cere_lan_ip }}:9140"`, bespoke task injecting `X-Midichlorian-Token` from SSM like BAU's proxy token, 0600, `no_log`), `docs/runbooks/ec2-dr/leia.md` list.

Copy `bau.home.nakomis.com.conf`: mTLS optional + `if ($ssl_client_verify != SUCCESS) return 401`, `{% if cere_lan_ip == 'unset' %}` placeholder 503; for `/api/runs/*/log` add `proxy_buffering off; proxy_read_timeout 3600s;` (SSE).

- [ ] dr-check, `--list-tasks`. Commit `feat(leia): midichlorian.home.nakomis.com vhost (HOME-510)`.

### Task B7: Docs and meta-mcp

- `docs/runbooks/midichlorian.md`: bring-up order (below), how to use, how to override a printer hold, recovering an interrupted run.
- `cere/CLAUDE.md`, `mace/CLAUDE.md`, `leia/CLAUDE.md`, top-level `CLAUDE.md` (deploys go through midichlorian once live).
- mcps `meta-mcp/config.toml.example`: `[[mcps]] name = "midichlorian"`, `argv = ["uv","run","--project","~/repos/nakomis/mcps/midichlorian","midichlorian-mcp"]`, env `MIDICHLORIAN_INSTANCES`, `MIDICHLORIAN_TOKEN`. (The live `config.toml` is edited at go-live, with Martin's OK.)
- [ ] Commit; push; PR `feat: midichlorian infrastructure (HOME-510)`.

## Go-live order (after Cere is set up)

1. Martin: fine-grained PAT → SSM `/midichlorian/github-token`; reserve Cere's LAN IP and set `cere_lan_ip`.
2. With OK: create `/midichlorian/api-token`, nakotp legacy params (B2), SSH keys (B3).
3. Merge mcps PR and home-infra PR (CI deploys the CDK stack).
4. From phi: run each Jedi's playbook once (authorises the runner keys, picks up the nakotp change) — luke, leia, cal, mace, rey, cere.
5. `scripts/cere-ansible.sh --tags midichlorian`; click Local Network "Allow" on Cere; `scripts/mace-ansible.sh --tags midichlorian`; `scripts/leia-ansible.sh` (vhost).
6. First live run via the MCP: `deploy(["rey"], tags=["failed-units"])`; check UI, callback, marker file.
7. Add the meta-mcp entry; announce to sessions that deploys go through midichlorian.
