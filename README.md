# beam-hermes

Beam (b1m.ai) plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
verified bucket-to-bucket transfers, fan-out to several destinations, and Beam
Rooms as native Hermes tools.

Beam coordinates data movement. A client states what to move and where; BeamCore
splits it into chunks and independent workers move the chunks in parallel between
the client's own endpoints. Documentation: https://docs.b1m.ai

## Tools

All tools belong to the `beam` toolset. Every handler returns JSON and reports
failures as `{"error": ..., "kind": ...}`.

| Tool | What it does |
| --- | --- |
| `beam_account` | Checks `BEAM_API_KEY` against the Beam API: account status, user type, key role and key prefix. Never returns the key. |
| `beam_transfer_estimate` | Reads the source object's size from its storage and estimates the cost (0.01 credit per GB delivered to each destination). Beam is not contacted. |
| `beam_transfer_start` | Starts a transfer in a detached background worker and returns its `job_id` and Beam `transfer_id`. Spends credits and overwrites destination objects; requires `confirmed: true`. |
| `beam_transfer_status` | Beam's status for a transfer, plus the worker's phase and last event for a job started here. |
| `beam_transfer_list` | The organization's recent transfers and the jobs started from this machine. |
| `beam_transfer_cancel` | Cancels a transfer. |
| `beam_rooms` | Operates Beam Rooms through the `beam` CLI: enroll the machine, list, show, members, channels, grants, invitations, join, publish, media ingest, create (1 credit, requires `confirmed: true`). |
| `beam_room_listen` | Starts, stops or lists background listeners on Room message channels. |
| `beam_room_messages` | Reads what a listener received, from a cursor. |

The plugin also ships a `beam` skill (`skill_view("beam:beam")`) with guidance on
when Beam fits, confirming costs, credentials, Rooms and error handling.

## Requirements

- Hermes Agent 0.21.6 or later.
- Python dependencies, installed by Hermes when the plugin is enabled:
  `beam-network-sdk[s3,r2]` and `httpx` (see `pyproject.toml`).
- A Beam API key from https://console.b1m.ai. Transfers and Room creation spend
  the organization's credits.
- For Rooms only: the `beam` CLI, installed with the official installer
  documented at https://docs.b1m.ai.

## Install

```bash
hermes plugins install Beam-Network/beam-hermes
hermes plugins enable beam
hermes config set BEAM_API_KEY b1m_...
```

To try a local checkout, copy it to `~/.hermes/plugins/beam` and run
`hermes plugins enable beam`. Start a new session afterwards: tools added by a
plugin appear from the next session.

## Storage credentials

The transfer tools sign storage access locally with the Beam SDK. They read the
credentials for each endpoint from environment variables named by its URI scheme:

| URI | Variables |
| --- | --- |
| `s3://bucket/key` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (+ `AWS_DEFAULT_REGION`, `AWS_SESSION_TOKEN`) |
| `r2://bucket/key` | `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ACCOUNT_ID` or `R2_ENDPOINT_URL` |
| `s3c://bucket/key` | `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` (+ `S3_REGION`, `S3_PROVIDER`) |
| `hippius://bucket/key` | `HIPPIUS_API_TOKEN` (+ `HIPPIUS_BASE_URL`) |
| `hf://[datasets/\|spaces/]org/repo[@revision]/path` | `HF_TOKEN` (+ `HF_ENDPOINT`) |

Append `?env=PREFIX` to a URI to read `PREFIX_<VAR>` instead, for example
`s3://bucket/key?env=DST` reads `DST_AWS_ACCESS_KEY_ID`. Store variables with
`hermes config set NAME value`. Keys must not be restricted to IP addresses or
networks: independent workers use the signed routes from their own hosts.

An agent without keys of its own can use Beam Studio instead, which keeps storage
credentials and exposes transfers through its MCP server. The bundled skill
explains that path.

## How transfers run

`beam_transfer_start` validates the URIs and credentials, then starts
`python -m beam_hermes.worker run ...` in its own session with only the
environment variables that transfer needs (the base variables a process needs to
run, `BEAM_API_KEY`, and the storage variables its URIs name). The worker creates
the transfer, re-signs routes and answers integrity checks until the transfer
ends, so it keeps running after the Hermes session that started it. Its events
are appended as JSON lines to a file under the plugin's data directory, which
`beam_transfer_status` reads.

## What the plugin does on your machine

- **Network.** HTTPS to the Beam API (`beamcore.b1m.ai`); the Beam SDK's TLS
  connection to Beam's transfer gateway while a transfer runs; metadata reads
  (object size) from the storage providers named in the URIs. Storage keys never
  leave the machine: the SDK signs short-lived, chunk-scoped routes locally.
- **Processes.** A detached Python worker per transfer, running until the
  transfer ends. A detached `beam ... listen` process per Room listener, running
  until stopped with `beam_room_listen`. Short-lived `beam --json ...` commands for
  Room actions and `ps` to confirm a process is still the one the plugin started
  before reporting on or stopping it.
- **Files.** Everything is written under Hermes's per-plugin data directory
  (`<hermes home>/plugin-data/beam/`), owner-only: transfer job records and event
  logs, listener message logs, Room invitation tokens (`invite`), and the WHIP
  URL and bearer token from `media_publish`. Tools return the paths of token files,
  never the tokens.
- **Credentials.** The plugin reads `BEAM_API_KEY` and the storage variables
  named by the URIs it is given. Room tools run the installed `beam` CLI, which
  uses this machine's Beam agent enrollment. `agent_connect` enrolls the machine
  with `BEAM_API_KEY`.
- **Spending.** `beam_transfer_start` and `beam_rooms` `create` spend
  organization credits and refuse to run without `confirmed: true`.
- No telemetry, no self-updates.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
ruff check . && ruff format --check .
```

The tests make no network calls. On a machine with Hermes installed,
`hermes plugins validate . --install-deps` and `hermes plugins doctor . --ci` run
the catalog checks.

## License

MIT. Copyright (c) 2026 Beam Network, Fusionblock LLC.
