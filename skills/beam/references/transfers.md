# Beam transfers reference

## How a transfer runs

1. The background worker started by `beam_transfer_start` (the Beam Python SDK)
   reads the source object's size and signs short-lived, chunk-scoped access to
   the source and every destination **in its own process**. Storage credentials
   are never sent to Beam or to workers.
2. BeamCore splits the object into chunks (40 MiB minimum, larger for big
   objects) and assigns them to competing orchestrators. Their workers read each
   chunk from the source and write it to every destination.
3. When every chunk is verified, BeamCore completes the destination objects and
   the transfer becomes `completed`.

While the transfer runs, the worker re-signs routes that expire and answers
integrity checks. That is why it must live until its final event.

## Credentials and permissions

Credentials must not be restricted to specific IP addresses or networks:
independent workers use the signed routes from their own hosts.

**Amazon S3 and S3-compatible stores** (`s3://`, `s3c://`). The source needs
`s3:GetObject`, plus `s3:GetObjectVersion` on a versioned bucket. The destination
needs:

```json
[
  {
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject",
               "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
    "Resource": "arn:aws:s3:::DESTINATION-BUCKET/*"
  },
  { "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::DESTINATION-BUCKET" }
]
```

The destination needs read and delete too: the SDK reads uploaded parts back to
check them, and cleans up temporary objects under `<destination-key>.beam-recovery/`.

**Cloudflare R2** (`r2://`). Use an R2 API token with **Object Read only** for a
source bucket and **Object Read & Write** for a destination bucket. The endpoint
is `https://<account-id>.r2.cloudflarestorage.com` unless `R2_ENDPOINT_URL` is set.

**Hippius** (`hippius://`). One API token. Workers never receive it.

**Hugging Face** (`hf://`). A token that can read the source repo and write to
the destination repo. Uploading to the Hub from a non-Hub source needs
`hf_allow_source_rehash: true`, because the Hub requires the file's sha256
before it issues upload URLs, so the SDK reads the source once.
`hf_commit_message` and `hf_create_pr` shape the commit. A Hugging Face
destination takes exactly one source.

**Different accounts on each side.** Append `?env=PREFIX` to a URI and the tools
read `PREFIX_<VAR>` for that endpoint, for example `s3://bucket/key?env=DST`
reads `DST_AWS_ACCESS_KEY_ID` and `DST_AWS_SECRET_ACCESS_KEY`.

## Statuses

| Status | Meaning |
| --- | --- |
| `pending` | Created, awaiting assignment |
| `in_progress` | Chunks are moving |
| `completed` | All chunks verified, destination objects complete |
| `failed` | Terminal error; read `error_message` |
| `cancelled` | Cancelled by the client or a timeout |

If an orchestrator or worker stalls, Beam reassigns its chunks automatically. No
action is needed.

## Worker events

`beam_transfer_status` returns the worker's `last_event`. Events, in order:
`started`, `created` (has `transfer_id`, `size_bytes`, `estimated_credits`),
`progress` (`status`, `bytes_transferred`, `total_bytes`, `percent`), then
exactly one final event:

| Event | Fields |
| --- | --- |
| `completed` | `size_bytes`, `delivered_bytes`, `destinations_completed`, `started_at`, `completed_at`, `integrity_check_warning` |
| `failed` | `error_message`, and `code`/`hint` for storage access errors |
| `cancelled` | `transfer_id` |
| `timeout` | The worker stopped waiting; the transfer may still finish |
| `interrupted` | The worker was stopped; the transfer was not cancelled |
| `error` | `kind` (`auth`, `insufficient_credits`, `create_rejected`, `missing_env`, ...) and `message` |

## Costs

- Transfer: **0.01 credit per GB delivered**, rounded up per transfer to the next
  0.01 credit. GB is decimal (10^9 bytes). 1 credit = $0.50, so $5 per TB.
- Fan-out delivers the object once per destination: 1 TB to 3 destinations is
  about 30 credits.
- Room start: 1 credit. A Studio workflow run: 1 credit plus its transfers.

`estimated_credits` uses this formula. The Console shows the actual charge. A
transfer that was already running when credits ran out still completes and is
charged, so the balance can go below zero.

## Limits

- **Size must be known.** The size is read from the source object, so the object
  must be complete. A growing or live source belongs on a Room channel.
- **Small objects gain nothing.** Below 40 MiB an object is a single chunk on a
  single worker. Bundle many small files into one archive first.
- **Whole objects only.** There is no sync, delta or deduplication. Work out what
  changed yourself and transfer those objects.
- **Cloudflare R2 range caveat.** Some completed R2 multipart objects with gaps in
  their part numbers return a truncated body for a range that crosses a part
  boundary. A transfer from such a source can fail on a length check. A good
  full-file checksum does not rule this out. Use independently uploaded,
  range-verified source objects.
- **Hippius verification differs.** Hippius uses non-multipart routes, so its
  completion checks differ from S3 multipart. Confirm the destination object
  afterwards if the user needs end-to-end verification.
- **Unsupported endpoints.** Google Cloud Storage and Azure Blob are not
  supported. Plain HTTP sources and destinations work only from code with the
  SDK's HTTP connector (https://docs.b1m.ai/docs/connectors/http).
