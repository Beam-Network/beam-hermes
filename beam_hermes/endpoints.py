"""Parse Beam endpoint URIs and build the SDK provider models for them.

Storage credentials come from environment variables named by each URI's scheme, optionally
behind a ``?env=PREFIX`` prefix. They are signed locally by the Beam SDK and never returned.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .common import BYTES_PER_GB, BeamToolError

SCHEMES = ("s3", "r2", "s3c", "hippius", "hf")

UNSUPPORTED_SCHEMES = {
    "gs": "Google Cloud Storage is not supported as a Beam transfer endpoint.",
    "gcs": "Google Cloud Storage is not supported as a Beam transfer endpoint.",
    "azure": "Azure Blob Storage is not supported as a Beam transfer endpoint.",
    "http": "HTTP endpoints need the SDK's HTTP connector (https://docs.b1m.ai/docs/connectors/http).",
    "https": "HTTP endpoints need the SDK's HTTP connector (https://docs.b1m.ai/docs/connectors/http).",
}

HF_TYPE_PREFIXES = {
    "models": "model",
    "datasets": "dataset",
    "spaces": "space",
    "kernels": "kernel",
}
HF_SPECIAL_REVISION = re.compile(r"^refs/(?:convert/[\w.-]+|pr/\d+)")
ENV_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass
class Endpoint:
    uri: str
    scheme: str
    prefix: str
    bucket: str = ""
    key: str = ""
    hf_repo_type: str = "model"
    hf_repo_id: str = ""
    hf_revision: str = "main"
    credentials: list[str] = field(default_factory=list)

    @property
    def basename(self) -> str:
        return posixpath.basename(self.key.rstrip("/"))

    @property
    def location(self) -> str:
        if self.scheme == "hf":
            kind = "" if self.hf_repo_type == "model" else f"{self.hf_repo_type}s/"
            return f"hf://{kind}{self.hf_repo_id}@{self.hf_revision}/{self.key}"
        return f"{self.scheme}://{self.bucket}/{self.key}"

    def env(self, name: str, *, required: bool = True) -> str | None:
        """Read ``<prefix><name>`` and record that this endpoint uses it."""
        variable = f"{self.prefix}{name}"
        value = (os.environ.get(variable) or "").strip()
        if value:
            if variable not in self.credentials:
                self.credentials.append(variable)
            return value
        if required:
            raise BeamToolError(
                "missing_env",
                f"{variable} is not set; it is required for {self.scheme}:// endpoints.",
                variable=variable,
                endpoint=self.uri,
            )
        return None


def parse_endpoint(raw: Any) -> Endpoint:
    if not isinstance(raw, str) or not raw.strip():
        raise BeamToolError("invalid_uri", "Each endpoint must be a non-empty URI string.")
    raw = raw.strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme in UNSUPPORTED_SCHEMES:
        raise BeamToolError("unsupported_scheme", UNSUPPORTED_SCHEMES[scheme], endpoint=raw)
    if scheme not in SCHEMES:
        raise BeamToolError(
            "unsupported_scheme",
            "Use s3://, r2://, s3c://, hippius:// or hf:// URIs.",
            endpoint=raw,
        )
    query = parse_qs(parts.query, keep_blank_values=True)
    unknown = sorted(set(query) - {"env"})
    if unknown or parts.fragment:
        raise BeamToolError("invalid_uri", "The only supported URI option is ?env=PREFIX.", endpoint=raw)
    prefix = ""
    if "env" in query:
        value = query["env"][0].strip().upper()
        if not ENV_PREFIX.match(value):
            raise BeamToolError("invalid_uri", "?env= takes a prefix such as SRC or DST.", endpoint=raw)
        prefix = f"{value}_"
    display = raw.split("?", 1)[0]
    if scheme == "hf":
        return parse_hf(display, prefix)
    bucket, _, key = f"{parts.netloc}{parts.path}".partition("/")
    if not bucket:
        raise BeamToolError("invalid_uri", f"{display} names no bucket.", endpoint=raw)
    return Endpoint(uri=display, scheme=scheme, prefix=prefix, bucket=bucket, key=key)


def parse_hf(uri: str, prefix: str) -> Endpoint:
    """Parse ``hf://[<type>/]<org>/<repo>[@<revision>]/<path>``."""
    rest = uri[len("hf://") :]
    repo_type = "model"
    head = rest.split("/", 1)
    if len(head) == 2 and head[0] in HF_TYPE_PREFIXES:
        repo_type = HF_TYPE_PREFIXES[head[0]]
        rest = head[1]
    revision = "main"
    if "@" in rest:
        repo_id, remainder = rest.split("@", 1)
        special = HF_SPECIAL_REVISION.match(remainder)
        if special:
            revision = special.group(0)
            path = remainder[len(revision) :].lstrip("/")
        else:
            revision, _, path = remainder.partition("/")
    else:
        segments = rest.split("/")
        repo_id, path = "/".join(segments[:2]), "/".join(segments[2:])
    if repo_id.count("/") != 1 or not all(repo_id.split("/")):
        raise BeamToolError("invalid_uri", f"{uri} must name the repo as <org>/<repo>.", endpoint=uri)
    return Endpoint(
        uri=uri,
        scheme="hf",
        prefix=prefix,
        key=path,
        hf_repo_type=repo_type,
        hf_repo_id=repo_id,
        hf_revision=revision,
    )


def source_model(endpoint: Endpoint) -> Any:
    if not endpoint.key or endpoint.key.endswith("/"):
        raise BeamToolError(
            "invalid_uri", f"Source {endpoint.uri} must name one object.", endpoint=endpoint.uri
        )
    return build_model(endpoint, source=True, hf_options={})


def destination_model(endpoint: Endpoint, source: Endpoint, hf_options: dict[str, Any]) -> Any:
    if not endpoint.key or endpoint.key.endswith("/"):
        endpoint.key = f"{endpoint.key}{source.basename}"
    return build_model(endpoint, source=False, hf_options=hf_options)


def build_model(endpoint: Endpoint, *, source: bool, hf_options: dict[str, Any]) -> Any:
    from beam_network_sdk import (
        HippiusProviderDestination,
        HippiusProviderSource,
        HuggingFaceProviderDestination,
        HuggingFaceProviderSource,
        R2ProviderDestination,
        R2ProviderSource,
        S3CompatibleProviderDestination,
        S3CompatibleProviderSource,
        S3ProviderDestination,
        S3ProviderSource,
    )

    e = endpoint.env
    if endpoint.scheme == "s3":
        model = S3ProviderSource if source else S3ProviderDestination
        return model(
            bucket=endpoint.bucket,
            key=endpoint.key,
            region=e("AWS_DEFAULT_REGION", required=False) or "us-east-1",
            access_key_id=e("AWS_ACCESS_KEY_ID"),
            secret_access_key=e("AWS_SECRET_ACCESS_KEY"),
            session_token=e("AWS_SESSION_TOKEN", required=False),
        )
    if endpoint.scheme == "r2":
        model = R2ProviderSource if source else R2ProviderDestination
        access_key_id = e("R2_ACCESS_KEY_ID")
        secret_access_key = e("R2_SECRET_ACCESS_KEY")
        endpoint_url = e("R2_ENDPOINT_URL", required=False)
        account_id = e("R2_ACCOUNT_ID", required=endpoint_url is None)
        return model(
            bucket=endpoint.bucket,
            key=endpoint.key,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            account_id=account_id,
            endpoint_url=endpoint_url,
        )
    if endpoint.scheme == "s3c":
        model = S3CompatibleProviderSource if source else S3CompatibleProviderDestination
        return model(
            provider=e("S3_PROVIDER", required=False) or "s3-compatible",
            endpoint_url=e("S3_ENDPOINT_URL"),
            region=e("S3_REGION", required=False),
            bucket=endpoint.bucket,
            key=endpoint.key,
            access_key_id=e("S3_ACCESS_KEY_ID"),
            secret_access_key=e("S3_SECRET_ACCESS_KEY"),
        )
    if endpoint.scheme == "hippius":
        model = HippiusProviderSource if source else HippiusProviderDestination
        return model(
            bucket=endpoint.bucket,
            key=endpoint.key,
            api_token=e("HIPPIUS_API_TOKEN"),
            base_url=e("HIPPIUS_BASE_URL", required=False) or "https://api.hippius.com",
        )
    hf = {
        "repo_id": endpoint.hf_repo_id,
        "path": endpoint.key,
        "repo_type": endpoint.hf_repo_type,
        "revision": endpoint.hf_revision,
        "token": e("HF_TOKEN"),
        "endpoint": e("HF_ENDPOINT", required=False) or "https://huggingface.co",
    }
    if source:
        return HuggingFaceProviderSource(**hf)
    return HuggingFaceProviderDestination(**hf, **hf_options)


@dataclass
class TransferSpec:
    source: Endpoint
    destinations: list[Endpoint]
    source_model: Any
    destination_models: list[Any]

    def describe(self) -> dict[str, Any]:
        def entry(endpoint: Endpoint) -> dict[str, Any]:
            return {"uri": endpoint.location, "env": sorted(set(endpoint.credentials))}

        return {
            "source": entry(self.source),
            "destinations": [entry(destination) for destination in self.destinations],
        }

    def credential_variables(self) -> list[str]:
        """Every environment variable the endpoints read, for the worker's environment."""
        names: set[str] = set(self.source.credentials)
        for destination in self.destinations:
            names.update(destination.credentials)
        return sorted(names)


def build_spec(source: Any, destinations: Any, hf_options: dict[str, Any] | None = None) -> TransferSpec:
    from pydantic import ValidationError

    if isinstance(destinations, str):
        destinations = [destinations]
    if not isinstance(destinations, list) or not destinations:
        raise BeamToolError("invalid_argument", "Give at least one destination URI.")
    source_endpoint = parse_endpoint(source)
    destination_endpoints = [parse_endpoint(raw) for raw in destinations]
    options = {
        "commit_message": None,
        "create_pr": False,
        "allow_source_rehash": False,
        **(hf_options or {}),
    }
    try:
        source_config = source_model(source_endpoint)
        destination_configs = [
            destination_model(destination, source_endpoint, options) for destination in destination_endpoints
        ]
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise BeamToolError("invalid_endpoint", problems) from None
    if len({d.location for d in destination_endpoints}) != len(destination_endpoints):
        raise BeamToolError("duplicate_destination", "Each destination must be a different object.")
    return TransferSpec(source_endpoint, destination_endpoints, source_config, destination_configs)


def estimated_credits(delivered_bytes: int) -> float:
    """0.01 credit per decimal GB delivered, rounded up per transfer to 0.01 credit."""
    return -(-delivered_bytes // BYTES_PER_GB) / 100
