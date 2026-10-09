---
name: beam
description: "Use when moving data with Beam: transfers, fan-out, Rooms. The beam_* tools estimate and confirm cost, then start, track and cancel verified copies between S3, R2, S3-compatible stores, Hippius and Hugging Face; Beam Rooms carry messages, live media and invitations between agents."
version: 0.1.0
author: Beam Network
license: MIT
metadata:
  hermes:
    tags: [data-transfer, object-storage, s3, r2, huggingface, rooms, agents]
    category: devops
---

# Beam

Beam (b1m.ai) coordinates data movement. A client states what to move and where;
BeamCore splits it into chunks and independent workers move the chunks in
parallel between the client's own endpoints. The `beam` plugin gives you Beam as
tools:

| Tool | Use |
| --- | --- |
| `beam_account` | Check `BEAM_API_KEY` (role and key prefix, never the key) |
| `beam_transfer_estimate` | Size and estimated credits; nothing is spent |
| `beam_transfer_start` | Start a transfer after the user approved the estimate |
| `beam_transfer_status` | Beam status plus the background worker's phase |
| `beam_transfer_list` | Recent transfers, and jobs started from this machine |
| `beam_transfer_cancel` | Cancel, only when the user asks |
| `beam_rooms` | Rooms: enroll this machine, list, show, channels, grants, invitations, join, publish, media |
| `beam_room_listen` | Start or stop a background listener on a message channel |
| `beam_room_messages` | Read what a listener received, from a cursor |

## When Beam fits, and when it does not

Use Beam for:

- Copying a large object between buckets, across providers or accounts.
- **Fan-out**: the same object to several destinations in one transfer (several
  entries in `destinations`).
- Rooms: several machines or parties exchanging messages, live media or files
  with roles and per-channel grants.

Say why Beam is the wrong tool when:

- The object is small. Chunks start at 40 MiB, so a small object is one chunk on
  one worker and gains nothing. Suggest bundling small files into an archive.
- The user needs sync or deltas. A transfer moves whole objects only.
- The size is unknown (a live feed or a growing log). That is a Room channel,
  not a transfer.
- The endpoint is Google Cloud Storage, Azure Blob or plain HTTP. The tools take
  only `s3://`, `r2://`, `s3c://`, `hippius://` and `hf://`.
- Someone outside must reach a private service. Beam tunnels are not available.

Beam is not compute, storage or a CDN.

## Which storage credentials

Storage keys are best kept in Beam Studio, which signs storage requests itself
so you never see them.

- **This agent's own keys**: when the variables for every source and
  destination are set (table below), use the transfer tools.
  `beam_transfer_estimate` names any missing one (`kind: missing_env`,
  `variable`).
- **Studio**: otherwise. If Studio's MCP tools are available (Hermes names them
  like `mcp__beam_studio__beam_list_credentials`), prefer them and read
  `references/studio.md` from this skill first. If they are not, ask the user to
  connect Studio's MCP server or to give this agent its own keys. Never ask the
  user to paste a storage key into the chat.
- One transfer uses one path: a Studio workflow cannot use this agent's keys,
  and these tools cannot use Studio's.

The operator gives this agent its own keys with `hermes config set NAME value`.

| URI | Variables |
| --- | --- |
| `s3://bucket/key` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (+ `AWS_DEFAULT_REGION`, `AWS_SESSION_TOKEN`) |
| `r2://bucket/key` | `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ACCOUNT_ID` or `R2_ENDPOINT_URL` |
| `s3c://bucket/key` (MinIO, Wasabi, B2, Spaces...) | `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` (+ `S3_REGION`, `S3_PROVIDER`) |
| `hippius://bucket/key` | `HIPPIUS_API_TOKEN` (+ `HIPPIUS_BASE_URL`) |
| `hf://[datasets/\|spaces/]org/repo[@revision]/path` | `HF_TOKEN` (+ `HF_ENDPOINT`) |

- Source and destination in different accounts: add `?env=SRC` or `?env=DST`
  to a URI, and the tools read `SRC_<VAR>` or `DST_<VAR>` for that endpoint.
- A destination key ending in `/` receives the source file name.

## Transfers

1. **Estimate.** `beam_transfer_estimate` with `source` and `destinations`. It
   reads the size from the source storage only.
2. **Confirm.** Tell the user the source, every destination (existing objects at
   those keys are overwritten), the size and `estimated_credits` (0.01 credit
   per GB delivered to each destination; 1 credit = $0.50). Wait for a clear yes.
3. **Start.** `beam_transfer_start` with the same URIs and `confirmed: true`.
   It returns a `job_id` and, once Beam accepts the transfer, its
   `transfer_id`. Report the `transfer_id`; the Beam Console lists every
   transfer.
4. **Track.** `beam_transfer_status` with the `job_id`. Phases: `starting`,
   `running`, then exactly one of `completed`, `failed`, `cancelled`, `timeout`,
   `error`, `interrupted`. `worker_exited` means the background worker stopped
   without a final event; the transfer was not cancelled, so check its Beam
   status.
5. **Cancel** only when the user asks: `beam_transfer_cancel`.

The background worker signs routes and answers integrity checks for the whole
transfer, and it keeps running after this session ends. Leave it alone until its
final event. For transfers that may take longer than six hours, pass a larger
`timeout_seconds`.

| Signal | Meaning | Next step |
| --- | --- | --- |
| `kind: missing_env` | A variable the URI needs is unset | Ask the operator to set the variable named in `variable`, or use Studio |
| `kind: source_unreadable` | The estimate could not read the source size | Check the URI, the credentials and the endpoint |
| `kind: auth` | Beam refused the API key, or it may not create transfers | A valid key from the Console's API Keys page |
| `kind: insufficient_credits` | The organization cannot pay for it | The user adds credits or turns on auto top-up in the Console |
| `error_message` starts with `source_access_denied` | The source refused Beam's signed reads | Check read permission; remove any IP or network restriction |
| `error_message` starts with `destination_access_denied` | The destination refused the writes | Check write and multipart permissions; see `references/transfers.md` |

Storage keys must not be restricted to IP addresses or networks: independent
workers use the signed routes from their own hosts. `references/transfers.md`
lists the permissions per provider, statuses, costs and limits.

## Rooms

- **Enroll this machine once.** `beam_rooms` with `action: agent_status`; if it
  is not connected, `action: agent_connect` (it uses `BEAM_API_KEY`). Never run
  `beam auth login` or `beam login`: they wait for a browser sign-in that an
  agent cannot complete.
- If the `beam` CLI is missing (`kind: beam_cli_missing`), ask the user to
  install it with the official installer documented at https://docs.b1m.ai.
  Install nothing without their approval.
- **Creating a Room costs 1 credit** and needs a key whose role grants
  `rooms:start`. Ask the user, then `action: create` with `confirmed: true`.
  Reuse the returned `idempotency_key` if you retry.
- **Channels** are restricted by default: a member needs `discover` plus
  `subscribe` to receive or `publish` to send. `grant_put` replaces the subject's
  whole action list, so read `grant_list` first and include what it already has.
- **Messages are live only.** Start `beam_room_listen` (`action: start`) before
  anyone publishes, then read with `beam_room_messages`, passing `next_cursor`
  each time. A Room needs a second member before a message can be delivered
  (`channel_no_recipient`). Stop listeners you no longer need.
- **Streams are currently unavailable.** For live audio or video use a `media`
  channel: `media_publish` writes the WHIP URL and its bearer token to an
  owner-only file for an encoder such as OBS or ffmpeg 8 or later. Give the user
  the file path; never print the token. Use `message` channels for events.
- **Inviting** another agent, a person or a machine: only with the user's
  approval, and only parties whose operators agreed to join (a member spends its
  own compute and model credits). `action: invite` with the fewest
  `channel_access` actions and `max_uses: 1`; bind it with `agent_id` when the
  invitee's ID is known. The tool returns an owner-only file path; deliver that
  file over a private channel the user approves. The invitee joins with
  `action: join` and `invitation_file`, and `remove_invitation_file: true`.

| `kind` | What to do |
| --- | --- |
| `room_api_key_permission` | Use a key whose role grants `rooms:start` |
| `room_credit_required` | The key or organization cannot pay; the user checks the Console |
| `room_create_requires_account_agent` | This machine joined by invitation only; it cannot create Rooms |
| `channel_not_visible` | Wrong channel ID, or missing `discover` grant |
| `channel_permission_denied` | Missing `publish` or `subscribe` grant |
| `channel_not_ready` | Encryption setup still converging; retry shortly |
| `channel_no_recipient`, `channel_not_delivered` | Nobody was listening; start a listener first |
| `agent_unavailable` | The local Beam agent is down; `agent_status`, then `agent_connect` |

## Rules

- Workers are independent third parties and can read the bytes of the chunks
  they move. For confidential data, encrypt it before transferring, or use an
  agent-only Room object publication (end-to-end encrypted).
- Never quote throughput, savings or delivery-time figures. Measure instead.
- Never claim support for an endpoint the tools reject.
- API keys, storage keys, invitation tokens and media tokens stay out of the
  chat, out of files you write and out of command arguments.
- Beam has one public environment. Never set `BEAM_ENV`, `BEAM_NATS_URL` or a
  development host.
- This skill and the plugin's files are read-only. Report problems to the user
  instead of editing them.
