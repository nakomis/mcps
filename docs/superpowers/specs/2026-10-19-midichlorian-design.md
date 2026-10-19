# midichlorian: a playbook-run coordinator for the Jedi

**Plane:** HOME-510 · **Date:** 19 Oct 2026 · **Status:** design agreed with Martin, awaiting spec review

## Why

Ansible runs against the Jedi (Luke, Leia, Cal, mace, Rey, Cere) are started by whichever Claude session needs one.
Coordination between them happens by sessions messaging each other, and by one session acting as the
de facto scheduler. That has gone wrong in ways that cost real time:

- runs deployed from a shared checkout that another session had switched to an old branch (22 commits behind main);
- merged changes that never reached a host because nobody ran its playbook (ATC-1 on Luke), so a dependent
  deploy on another host failed;
- order-sensitive rollouts (Luke, then Leia, then mace) relayed by hand between sessions;
- runs paused or rescheduled around 3D prints by sessions polling Cthulhu themselves;
- expired AWS SSO, a stopped Docker Desktop, and a stale Luke backup marker each discovered mid-run.

midichlorian replaces that with one service that owns the queue, the gates, the run and the verification,
and tells the requesting session when it is done.

## Goals

1. Any Claude session can ask for a host (or an ordered list of hosts) to be deployed at a commit, with optional
   Ansible tags, and is told the outcome without polling.
2. A request that is already satisfied (the host's last successful deploy includes the commit) returns at once.
3. Runs never disturb a 3D print or its recording unless Martin explicitly authorises it for that request.
4. Every run deploys merged code from `origin/main`, never someone's working checkout.
5. Martin can see the queue, rollout history, the state of each Jedi, and live logs at
   `https://midichlorian.home.nakomis.com`.

## Non-goals

- Deploying automatically on merge to main.
- Stopping a playbook part-way through (it could leave `/` writable).
- Per-user authentication beyond Leia's mTLS client certificate and a shared API token.
- Carrying Martin's approvals: the API never stands in for him. The one approval-shaped input, `override_printer`,
  is sent only when Martin has said so in a session.

## Architecture

```
 Mac (phi / mu)                                   LAN
┌────────────────────────────┐      HTTP+token   ┌──────────────────────────────┐
│ Claude session             │ ────────────────▶ │ midichlorian-server on Cere   │──▶ ansible → Luke, Leia,
│  └ meta-mcp                │                   │  (serves luke leia cal mace   │            Cal, mace, Rey
│     └ midichlorian MCP ────┼── result ─┐       │   rey; hosts the web UI)      │
│        (holds the session's│  pushed to│       └──────────────┬───────────────┘
│         messaging socket)  │◀──────────┘                      │ aggregates
└────────────────────────────┘                   ┌──────────────▼───────────────┐
                                                 │ midichlorian-server on mace   │──▶ ansible → Cere
 Browser ──mTLS──▶ Leia nginx ──▶ Cere :<port>   │  (serves cere only)           │
                                                 └──────────────────────────────┘
```

### Components

1. **`midichlorian-server`**: a Python (FastAPI) service with an HTTP API, a per-host queue and a runner.
   - Two instances. **Cere** is the primary and serves `luke`, `leia`, `cal`, `mace` and `rey`; it also serves the
     web UI. **mace** serves only `cere`. A host never runs its own playbook. Each instance refuses requests for
     hosts it does not serve.
   - Cere is a Mac like phi, so the existing playbooks run unchanged there (Homebrew tools, `flock`, Colima with
     Rosetta for the amd64 image builds). Cere's playbook has four controller-side tasks, needing only `aws` and
     `gh`, so it runs from Linux on mace.
   - LAN-only HTTP with a shared bearer token (SSM `/midichlorian/api-token`); no TLS on the LAN, as for the other
     intra-LAN links.
   - Runs as a launchd daemon on Cere and as a Docker container (or systemd service) on mace, with its data on a
     writable volume (`/mnt/data/midichlorian` on mace).
2. **`midichlorian` MCP**: a stdio FastMCP server launched by meta-mcp on the Mac.
   - Mirrors the HTTP API one tool per endpoint, and routes each request to the instance that serves the host.
   - Inherits the session's `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` from its environment,
     waits for its jobs, and delivers the result into the session (see Callbacks). The token never leaves the Mac.
3. **Runner environment on each runner host**:
   - a dedicated clone of `nakomis/home-infra`, reset to `origin/main` (or the requested commit's descendant on
     main) before every run, used by nothing else;
   - AWS profiles `nakom.is-admin` and `mh` whose `credential_process` is the IAM Roles Anywhere signing helper,
     using the host's own certificate (the naktv-devmode pattern). Each account gets a deploy role with the
     SSM, S3 and other read permissions the playbooks need; the profile names are unchanged, so no playbook edits;
   - a GitHub token from SSM, for Cere's playbook's `gh` task;
   - SSH access to the hosts it serves, and the host-specific helpers the deploy scripts already expect.
4. **Infrastructure** (in `nakomis/home-infra`): the Roles Anywhere roles and trust (CDK), the Cere and mace
   Ansible tasks that install the server, and Leia's `midichlorian.home.nakomis.com` mTLS vhost pointing at Cere.

Code lives in `mcps/midichlorian/`: one uv project (hatchling), two entry points (`midichlorian-server`,
`midichlorian-mcp`), pytest, matching the other MCPs in the repo.

Prior art: `claude-bus-mcp` (a NATS-based lease and messaging bus) is not used; midichlorian owns its queue
directly and needs no shared bus.

## A run, step by step

1. **Request.** `POST /api/deploy` with `{hosts: [...], sha, tags?, override_printer?, requester}`.
   `sha` must be on `origin/main` (refused otherwise). Several hosts mean "in this order".
2. **Already deployed?** Per host, if the last successful *full* deploy's commit has `sha` as an ancestor (or, for a
   tagged request, the last successful run that covered those tags), the host is reported as already deployed and
   skipped.
3. **Queue.** One run per host at a time. A new request merges into a queued (not yet running) job for the same
   host where it can: a full run covers any tags; a run at `origin/main` covers any older commit. Every merged
   requester gets the result. A multi-host request waits for each host to pass before starting the next.
4. **Gates**, checked when the job reaches the front of its queue:
   - **AWS**: `sts get-caller-identity` for each profile. Failure fails the job at once, with the error.
   - **Printer hold**: if either printer is busy (Cthulhu `GET :9120/api/status` on Rey; OctoPrint `GET /api/job`
     on Leia), hold runs on **Rey**, **Leia**, and **Luke runs that would restart Samba**, until both are idle.
     The requester is told it is waiting. `override_printer: true` lifts the hold for that request only.
     A running job is never interrupted. ScreenEye saves frames only on a button press, so it needs no hold.
   - **Luke's backups**: midichlorian runs `ssh luke ./run-backups.sh` itself, immediately before a Luke run. A
     failed backup fails the job, and Luke is not touched.
   - **Docker** on the runner, for hosts whose playbook builds images (Rey, Luke's volatile-fs artefact).
5. **Run.** Refresh the checkout, run `scripts/<host>-ansible.sh [--tags …]`, and stream the full output to the
   run's log file.
6. **Verify.**
   - the PLAY RECAP shows `failed=0` and `unreachable=0`;
   - `/` is read-only on the ro-root hosts (Luke, Leia, Cal, mace);
   - `systemctl --failed` is empty;
   - every container's status, health and restart count, compared with before the run;
   - if Luke's Samba restarted, the `/mnt/print-recordings` CIFS mounts on Rey, Leia and mace are checked for
     stale handles and remounted with `umount -l` (deferred on Rey while a print is running).
7. **Record.** On success, write the commit to `/var/log/midichlorian/deployed` on the host (`/var/log` is
   writable on the ro-root boxes) and to the server's history.
8. **Reply.** Push the outcome to every requester: success with the verification summary, or failure with the
   failing task and the log's tail.

## HTTP API (shared by the web UI and the MCP)

| Method and path | Purpose |
|---|---|
| `GET /api/hosts` | Per Jedi: deployed commit and time, last result, serving instance, current gates (printer busy, Luke backups fresh), whether a run is in progress |
| `GET /api/queue` | Queued and running jobs |
| `GET /api/runs?host=&limit=` | Rollout history: requester, commit, tags, result, duration, verification summary |
| `GET /api/runs/{id}` | One run in full, with every verification check |
| `GET /api/runs/{id}/log` | The log. `?follow=1` streams live over Server-Sent Events; `?since=<offset>` pages |
| `POST /api/deploy` | Start a deploy. Returns a job id per host, or "already deployed" |
| `POST /api/runs/{id}/cancel` | Cancel a queued job. A running job cannot be cancelled |

The Cere instance's `GET` endpoints include the mace instance's data, so the UI and the MCP see the whole estate
from one place.

## MCP tools

`hosts`, `queue`, `runs`, `run`, `run_log` (a page at a time), `deploy`, `cancel`, one per endpoint above.
`deploy` returns the job ids at once and arranges the callback. `requester` is filled in by the MCP from the
session's name.

## Callbacks

The MCP, not the server, delivers results: the server cannot reach a Unix socket on the Mac, and the session's
messaging token must not leave it.

- For each job it has submitted, the MCP follows the job's state over the API and, when the job finishes (or starts
  waiting on a printer), writes the documented auth line followed by a user message to
  `$CLAUDE_CODE_MESSAGING_SOCKET`.
- The message payload line is undocumented Claude Code internals (found in v2.1.303's help text, needs
  v2.1.224+). It is isolated in one module so it can be swapped for an MCP channel
  (`notifications/claude/channel`) if the format changes.
- A result lands as a cross-session message, not user input; the receiving session treats it as a teammate's
  report. If the session has gone, the result stays in the history.

## Web UI: `midichlorian.home.nakomis.com`

A single static page (plain JavaScript, no build step) served by the Cere instance, behind Leia's nginx with the
usual mTLS client-certificate check.

- a card per Jedi: deployed commit and time, last result, gate badges (for example "printer busy"), a live-run indicator;
- the live queue;
- rollout history, each row opening the run's checks and full log;
- for a run in progress, its log streaming live.

## Failure handling

| Situation | Behaviour |
|---|---|
| Playbook fails or a host is unreachable | Run marked failed, log kept, nothing recorded as deployed. A multi-host request stops; later hosts do not run. The requester gets the failing task and the log's tail. |
| `/` still writable after the run | Run marked failed even if the playbook passed, and shown in red. Not auto-fixed: the cause decides the fix. |
| AWS gate fails | Job fails at once with the error. |
| Printer busy | Job waits; requester told. |
| Luke's backups fail | Job fails; Luke untouched. |
| Server restarts mid-run | On start-up, any job still marked running becomes "interrupted" and is not re-run; the UI says to check the host. |
| Requester's session gone | Result stays in history; delivery failure is logged, not an error. |
| Commit not on `origin/main` | Refused. |
| Host served by the other instance | The MCP routes it; a server receiving one directly refuses it. |

## State

- Each server keeps its queue and history in SQLite and its run logs as files, in its own data directory.
- The deployed commit is also on each host (`/var/log/midichlorian/deployed`), so a lost database can be rebuilt
  from the hosts.

## Testing

- **Unit tests** (pytest): queue merging and ordering; the ancestor check against a throwaway git repo; gate logic
  with faked Cthulhu, OctoPrint and backup responses; PLAY RECAP and check parsing; the API through FastAPI's test
  client; the MCP's socket delivery against a fake Unix socket.
- **No real hosts in tests**: the playbook runner is an interface, and tests use a fake that writes a scripted log
  and exit code.
- **First live run**: a harmless tag-scoped run (for example `--tags failed-units` on Rey) before sessions switch over.

## Open points for the implementation plan

- The exact IAM permissions for the deploy roles (derive from every `aws` call in the playbooks and scripts).
- How each playbook marks a "Samba would restart" change, so the printer hold on Luke can be decided before the
  run (a `--check` pass, or treating every full Luke run as Samba-affecting).
- The Cere instance's port, and the order of rollout: Cere must be provisioned and running CI before it takes on
  deploys.
