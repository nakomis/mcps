# midichlorian

A playbook-run coordinator for Martin's home servers (HOME-510). Claude sessions
ask it to run a host's Ansible playbook at a commit of `origin/main` of
`nakomis/home-infra`; it queues the runs, applies the safety gates below, runs
the playbooks one host at a time and tells the requesting session how each run
ended. A small web UI shows the queue and run logs.

Two entry points:

| Command | Runs on | Does |
|---|---|---|
| `midichlorian-server` | the coordinating hosts | FastAPI service (queue, gates, playbook runner, UI) on port 9140 |
| `midichlorian-mcp` | the Mac, via meta-mcp | stdio MCP server: `hosts`, `queue`, `runs`, `run`, `run_log`, `deploy`, `cancel` |

## Architecture

Two server instances, each serving the hosts the other cannot run for:

- the **Cere** instance serves `luke`, `leia`, `cal`, `mace`, `rey` and the web UI;
- the **mace** instance serves `cere`.

No host ever runs its own playbook. The instances know each other as peers; the
first instance listed in the MCP's `MIDICHLORIAN_INSTANCES` is the primary and
merges its peers' reads. A `deploy` that spans both instances is submitted in
groups, each only after the previous group succeeded. If the first host of a
request fails, the later hosts are marked `skipped` and every requester is told.

When a run ends (or first starts waiting on a printer), the MCP pushes a message
beginning `[midichlorian]` into the requesting Claude session, so there is no
need to poll.

## Server configuration

Read by `midichlorian-server` (`config.py`, `api.py`, `runner.py`).

| Variable | Required | Meaning |
|---|---|---|
| `MIDICHLORIAN_INSTANCE` | yes | This instance's name (for example `cere`). Must not also appear in `MIDICHLORIAN_SERVED` |
| `MIDICHLORIAN_SERVED` | yes | Comma-separated hosts this instance runs playbooks for |
| `MIDICHLORIAN_DATA_DIR` | yes | Directory for the SQLite database (`midichlorian.db`) |
| `MIDICHLORIAN_REPO_DIR` | yes | The server's clone of home-infra, fetched before each commit check |
| `MIDICHLORIAN_REPO_URL` | no | Repository to clone from (default `https://github.com/nakomis/home-infra.git`) |
| `MIDICHLORIAN_TOKEN` | yes | API bearer token (a 64-hex-character token, by convention; held in SSM) |
| `MIDICHLORIAN_PEERS` | no | `name=url,name=url` of the other instances |
| `MIDICHLORIAN_BIND` | no | Listen address (default `0.0.0.0`) |
| `MIDICHLORIAN_PORT` | no | Listen port (default `9140`) |
| `MIDICHLORIAN_OCTOPRINT_KEY` | no | OctoPrint API key for the printer-busy check; if unset it is read from SSM. If neither yields a key, it is asked for again at most every 5 minutes; until then, while OctoPrint's container is running, the Prusa's state is unreadable and Leia's runs wait |
| `GH_TOKEN` | no | Not read by name: the server's environment is inherited by gates and playbook runs, so a GitHub token set here is what git uses |
| `AWS_CONFIG_FILE` | no | Not read by name: inherited by the SSM reads and playbook runs like the rest of the environment |

## MCP configuration

Read by `midichlorian-mcp`.

| Variable | Meaning |
|---|---|
| `MIDICHLORIAN_INSTANCES` | `name=url,name=url`; the first is the primary |
| `MIDICHLORIAN_TOKEN` | The API bearer token |
| `CLAUDE_SESSION_NAME` | Optional. The requester name for deploys; if unset, `session-<pid>` |
| `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN` | Inherited from the Claude session and used to push results back into it. The token stays on the Mac and is never sent to the server or logged |

An example meta-mcp entry is in `meta-mcp/config.toml.example`.

## Running locally

A throwaway dev instance with temporary directories:

```sh
cd midichlorian
export MIDICHLORIAN_INSTANCE=dev MIDICHLORIAN_SERVED=luke \
  MIDICHLORIAN_DATA_DIR=$(mktemp -d) MIDICHLORIAN_REPO_DIR=$(mktemp -d) \
  MIDICHLORIAN_TOKEN=$(openssl rand -hex 32) MIDICHLORIAN_BIND=127.0.0.1
uv run midichlorian-server
```

Then `curl http://127.0.0.1:9140/healthz`. Playbook runs need a real home-infra
checkout, so a dev instance is only useful for the API, UI and queue.

## Tests

```sh
cd midichlorian && uv run pytest -q
```

## Safety rules

- Deploys come only from commits on `origin/main` of home-infra. The server
  fetches before checking, so a commit newer than its clone is accepted.
- Nothing runs against a printer-sensitive target (`rey`, `leia` and `luke`) while
  a 3D print is in progress; the run waits. Every Luke run holds, full or tagged,
  because many Luke tags (`volatile-fs`, `print-recordings`, Docker restarts)
  bounce Samba and stale the print-recording mounts. The only exception is a run
  whose tags are all in `LUKE_HOLD_FREE_TAGS` (`hosts.py`), which is empty until
  a tag has been checked not to touch Samba, volatile-fs, print-recordings or
  Docker. A printer that
  cannot be reached (connection refused or timed out, ssh down, OctoPrint's
  container not running) is treated as idle, with a note. A printer that answers
  but whose state cannot be read (no or wrong OctoPrint key, an HTTP error, junk)
  holds the run as if busy ("state unreadable") until it can be read or Martin
  overrides.
- `override_printer` bypasses that hold. A session sets it only when Martin has
  explicitly said so in that session.
- A running playbook is never interrupted. Only queued or waiting runs can be
  cancelled. Stopping or restarting the server does not stop a playbook either:
  it runs in its own session, writing straight to its log, and finishes on its
  own; the restarted server marks the run `interrupted` (check the host), keeps
  its checkout until the process has gone, and carries on with the queue.
- Each instance holds a deploy of its peer's own host (`cere` on mace, `mace` on
  Cere), which restarts the peer, while the peer has a running job. An
  unreachable peer is not waited for.
- A second request for a host that is already running queues behind it.
- Run output in callback messages (log tails, task names, errors) is attacker-
  influenced, so it is fenced in a code block labelled as untrusted data.

## Deployment

Hosts, services, SSM parameters and rollout are in the home-infra repo, under
`docs/runbooks/midichlorian.md`.
