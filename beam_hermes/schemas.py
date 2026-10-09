"""Tool schemas: what the model reads to decide when and how to call each Beam tool."""

from __future__ import annotations

from typing import Any

URI_HELP = (
    "Object URI: s3://bucket/key, r2://bucket/key, s3c://bucket/key (MinIO, Wasabi, B2, "
    "Spaces...), hippius://bucket/key or hf://[datasets/|spaces/]org/repo[@revision]/path. "
    "Credentials come from environment variables named by the scheme (AWS_*, R2_*, S3_*, "
    "HIPPIUS_API_TOKEN, HF_TOKEN); append ?env=PREFIX to read PREFIX_<VAR> instead, for a "
    "second account."
)

ENDPOINT_PROPERTIES: dict[str, Any] = {
    "source": {"type": "string", "description": f"Source object. {URI_HELP}"},
    "destinations": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "description": "One or more destination object URIs (same forms as source). Several "
        "destinations make one fan-out transfer. A key ending in / receives the source file name.",
    },
    "hf_commit_message": {"type": "string", "description": "Commit summary for hf:// destinations."},
    "hf_create_pr": {"type": "boolean", "description": "Open hf:// uploads as a pull request."},
    "hf_allow_source_rehash": {
        "type": "boolean",
        "description": "Let the SDK read the source once to hash it; required to upload a "
        "non-Hub source to hf://.",
    },
}

ACCOUNT = {
    "name": "beam_account",
    "description": (
        "Check the Beam API key (BEAM_API_KEY) against https://beamcore.b1m.ai: returns the "
        "account status, user type, the key's role and its prefix, never the key. Call it "
        "first when Beam is set up for the first time or when another Beam tool reports an "
        "auth error."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

TRANSFER_ESTIMATE = {
    "name": "beam_transfer_estimate",
    "description": (
        "Estimate a Beam transfer before starting it: reads the source object's size from its "
        "storage and returns size_bytes, delivered_bytes and estimated_credits (0.01 credit per "
        "GB delivered to each destination; 1 credit = $0.50). Beam is not contacted and nothing "
        "is spent. Also validates every URI and that the credentials it needs are set. Show the "
        "result to the user and get their approval before beam_transfer_start."
    ),
    "parameters": {
        "type": "object",
        "properties": ENDPOINT_PROPERTIES,
        "required": ["source", "destinations"],
    },
}

TRANSFER_START = {
    "name": "beam_transfer_start",
    "description": (
        "Start a Beam transfer: copy one object to one or more destinations (fan-out). This "
        "SPENDS organization credits and OVERWRITES any existing objects at the destination "
        "keys, so call it only after beam_transfer_estimate and the user's explicit approval of "
        "the source, the destinations and the estimated credits, and pass confirmed=true. "
        "Starts a background worker that keeps running after this session until the transfer "
        "ends; returns a job_id and the Beam transfer_id. Track it with beam_transfer_status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **ENDPOINT_PROPERTIES,
            "confirmed": {
                "type": "boolean",
                "description": "Set to true only after the user approved this exact transfer and its estimated cost.",
            },
            "name": {"type": "string", "description": "Transfer name shown in the Beam Console."},
            "timeout_seconds": {
                "type": "integer",
                "description": "How long the worker waits for completion (default 21600, 6 hours).",
            },
            "wait_seconds": {
                "type": "integer",
                "description": "How long to wait here for Beam to accept the transfer (default 60, max 300).",
            },
        },
        "required": ["source", "destinations", "confirmed"],
    },
}

TRANSFER_STATUS = {
    "name": "beam_transfer_status",
    "description": (
        "Check a Beam transfer: Beam's status (pending, in_progress, completed, failed, "
        "cancelled; bytes_transferred, total_bytes, error_message) and, for a job started here, "
        "the background worker's phase and last event. Give the job_id from beam_transfer_start, "
        "a transfer_id, or both. On failed, error_message starting with source_access_denied or "
        "destination_access_denied means the storage key lacks permission or is IP-restricted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job ID returned by beam_transfer_start."},
            "transfer_id": {"type": "string", "description": "Beam transfer ID."},
            "include_metadata": {
                "type": "boolean",
                "description": "Also return Beam's internal transfer metadata (large; rarely needed).",
            },
        },
        "required": [],
    },
}

TRANSFER_LIST = {
    "name": "beam_transfer_list",
    "description": (
        "List the organization's recent Beam transfers (newest first) and the transfer jobs "
        "started from this machine. Use it to find a transfer_id or job_id again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Number of transfers (default 20, max 100)."},
            "offset": {"type": "integer", "description": "Skip this many transfers."},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "failed", "cancelled"],
                "description": "Only transfers with this status.",
            },
            "order_by": {"type": "string", "enum": ["created_at", "completed_at"]},
            "completed_after": {
                "type": "string",
                "description": "ISO 8601 time; with order_by=completed_at, only transfers finished after it.",
            },
            "include_local_jobs": {
                "type": "boolean",
                "description": "Include jobs started from this machine (default true).",
            },
        },
        "required": [],
    },
}

TRANSFER_CANCEL = {
    "name": "beam_transfer_cancel",
    "description": (
        "Cancel a running Beam transfer. Only when the user asks for it. Give the transfer_id "
        "or the job_id from beam_transfer_start; a job's worker then exits on its own."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "transfer_id": {"type": "string", "description": "Beam transfer ID."},
            "job_id": {"type": "string", "description": "Job ID returned by beam_transfer_start."},
        },
        "required": [],
    },
}

ROOMS = {
    "name": "beam_rooms",
    "description": (
        "Operate Beam Rooms through the beam CLI on this machine: private groups of agents, "
        "people and buckets exchanging messages, media and files over typed channels with "
        "per-channel grants. Actions: agent_status and agent_connect (enroll this machine "
        "with BEAM_API_KEY; required once before any Room action), list, show, member_list, "
        "channel_list, channel_create, grant_list, grant_put (replaces the subject's action "
        "list, so include actions it already has), invite (one-use invitation written to an "
        "owner-only file; returns the path, never the token), join (with an invitation file), "
        "publish (live message: start beam_room_listen on the receiving side first), "
        "media_publish (WHIP ingest for OBS/ffmpeg, saved to an owner-only file; returns the "
        "path, never the token), and create (costs 1 credit: ask the user first and pass "
        "confirmed=true). Stream channels are currently unavailable; use a media channel for "
        "live audio or video and a message channel for events."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "agent_status",
                    "agent_connect",
                    "list",
                    "create",
                    "show",
                    "channel_list",
                    "channel_create",
                    "grant_list",
                    "grant_put",
                    "invite",
                    "join",
                    "publish",
                    "member_list",
                    "media_publish",
                ],
            },
            "room": {
                "type": "string",
                "description": "Room ID (every action except agent_*, list and create).",
            },
            "channel": {
                "type": "string",
                "description": "Channel ID (grant_list, grant_put, publish, media_publish).",
            },
            "state": {
                "type": "string",
                "enum": ["active", "closed", "all"],
                "description": "list: which Rooms (default active).",
            },
            "confirmed": {
                "type": "boolean",
                "description": "create: true only after the user approved spending 1 credit.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "create/publish: reuse the same key on a retry so the action happens once.",
            },
            "label": {"type": "string", "description": "agent_connect: human-readable machine label."},
            "name": {"type": "string", "description": "channel_create: channel name."},
            "kind": {
                "type": "string",
                "enum": ["message", "stream", "datagram", "request-reply", "object", "media"],
                "description": "channel_create: channel kind (default message).",
            },
            "visibility": {
                "type": "string",
                "enum": ["restricted", "room"],
                "description": "channel_create: restricted (default) needs per-member grants.",
            },
            "persistence_modes": {
                "type": "array",
                "items": {"type": "string", "enum": ["none", "sender_local", "receiver_local"]},
                "description": "channel_create: allowed message retention modes.",
            },
            "persistence_max_bytes": {
                "type": "integer",
                "description": "channel_create: local retention byte quota.",
            },
            "persistence_max_age_seconds": {
                "type": "integer",
                "description": "channel_create: maximum local retention age.",
            },
            "subject_type": {
                "type": "string",
                "enum": ["member", "role"],
                "description": "grant_put: who receives the grant (default member).",
            },
            "subject": {"type": "string", "description": "grant_put: member or role ID."},
            "actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "grant_put: the complete action list, e.g. [discover, subscribe] to "
                "receive, add publish to send, manage to administer.",
            },
            "channel_access": {
                "type": "array",
                "items": {"type": "string"},
                "description": "invite: CHANNEL_ID=discover,subscribe entries; grant only what the invitee needs.",
            },
            "roles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "invite: role IDs granted on join.",
            },
            "max_uses": {"type": "integer", "description": "invite: uses (default 1)."},
            "ttl_seconds": {"type": "integer", "description": "invite: lifetime in seconds (default 900)."},
            "agent_id": {
                "type": "string",
                "description": "invite: bind the invitation to this agent ID so a leaked file is useless to others.",
            },
            "principal_id": {
                "type": "string",
                "description": "invite: bind the invitation to this principal ID.",
            },
            "invitation_file": {
                "type": "string",
                "description": "join: path of the invitation file received.",
            },
            "remove_invitation_file": {
                "type": "boolean",
                "description": "join: delete the invitation file after a successful join.",
            },
            "message": {
                "type": "string",
                "description": "publish: payload, up to 1 MiB; send JSON as a string.",
            },
            "content_type": {"type": "string", "description": "publish: content type (default text/plain)."},
            "retention": {
                "type": "string",
                "enum": ["none", "sender_local"],
                "description": "publish: none (live only, default) or sender_local on channels that allow it.",
            },
            "publisher": {
                "type": "string",
                "description": "media_publish: publisher name (default main); each name has its own URL and token.",
            },
            "verbose": {
                "type": "boolean",
                "description": "Return the CLI's full JSON (transport limits, versions, leases). Leave "
                "false unless a field you need is missing from the compact view.",
            },
        },
        "required": ["action"],
    },
}

ROOM_LISTEN = {
    "name": "beam_room_listen",
    "description": (
        "Start, stop or list background listeners on Beam Room message channels. A listener "
        "runs `beam room ROOM channel CHANNEL listen` detached and stores each message as one "
        "JSON line; read them with beam_room_messages. Messages are live only: start the "
        "listener before anyone publishes. Listening needs the subscribe grant (plus discover "
        "on a restricted channel). Stop listeners you no longer need."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "list"]},
            "room": {"type": "string", "description": "start: Room ID."},
            "channel": {"type": "string", "description": "start: channel ID."},
            "retention": {
                "type": "string",
                "enum": ["none", "receiver_local"],
                "description": "start: receiver_local keeps a local inbox on channels that allow it.",
            },
            "listener_id": {"type": "string", "description": "stop: listener ID returned by start."},
        },
        "required": ["action"],
    },
}

ROOM_MESSAGES = {
    "name": "beam_room_messages",
    "description": (
        "Read messages a beam_room_listen listener received, from a cursor. Pass next_cursor "
        "from the previous call to get only new messages; cursor 0 reads from the start."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "listener_id": {"type": "string", "description": "Listener ID returned by beam_room_listen."},
            "cursor": {
                "type": "integer",
                "description": "Byte offset from a previous next_cursor (default 0).",
            },
            "limit": {"type": "integer", "description": "Maximum messages (default 50, max 500)."},
            "max_bytes": {"type": "integer", "description": "Approximate response size cap (default 65536)."},
        },
        "required": ["listener_id"],
    },
}
