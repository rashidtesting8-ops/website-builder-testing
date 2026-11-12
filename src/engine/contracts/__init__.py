"""Pydantic v2 stage I/O, asset provenance, claim provenance, and warning contracts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ClaimStatus = Literal["verified", "needs_review", "expired"]
DemoKind = Literal["fictional", "third_party_internal_demo"]
DEMO_BANNER_TEXT = "INTERNAL DEMO - NOT A VERIFIED BUSINESS"
DEMO_BANNER_ATTRIBUTE = 'data-editvista-demo="true"'


class DemoMetadata(BaseModel):
    """Project-local classification for an internal demo fixture."""

    model_config = ConfigDict(extra="forbid")

    is_fixture: StrictBool
    kind: DemoKind
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be empty or whitespace-only")
        return value.strip()


def read_demo_metadata(project_root: str | Path) -> DemoMetadata | None:
    """Read and validate the optional demo classification from this project's input.json."""
    input_path = Path(project_root) / "input.json"
    metadata: DemoMetadata | None = None
    if input_path.is_file():
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid input.json at {input_path}: {exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError(f"Invalid input.json at {input_path}: expected a JSON object")
        if "demo" in payload:
            try:
                metadata = DemoMetadata.model_validate(payload["demo"])
            except ValidationError as exc:
                raise ValueError(f"Invalid demo metadata in {input_path}: {exc}") from exc

    state_path = Path(project_root) / ".editvista-demo-mode.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid demo state at {state_path}: {exc}") from exc
        if not isinstance(state, dict) or "demo" not in state:
            raise ValueError(f"Invalid demo state at {state_path}: expected a demo field")
        saved_demo = state["demo"]
        if saved_demo is not None:
            try:
                saved_metadata = DemoMetadata.model_validate(saved_demo)
            except ValidationError as exc:
                raise ValueError(f"Invalid demo state at {state_path}: {exc}") from exc
            saved_demo = saved_metadata.model_dump(mode="json")
        current_demo = metadata.model_dump(mode="json") if metadata is not None else None
        if saved_demo != current_demo:
            raise ValueError(
                f"Demo classification changed for {Path(project_root)}; use a new project ID"
            )

    return metadata


def freeze_demo_metadata(project_root: str | Path) -> DemoMetadata | None:
    """Persist this project's first demo classification and reject later relabeling."""
    root = Path(project_root)
    metadata = read_demo_metadata(root)
    state_path = root / ".editvista-demo-mode.json"
    if state_path.is_file():
        return metadata

    root.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {"demo": metadata.model_dump(mode="json") if metadata is not None else None},
        indent=2,
        sort_keys=True,
    )
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.",
        suffix=".tmp",
        dir=str(root),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, state_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return metadata


def has_demo_banner(html: str) -> bool:
    """Return whether HTML contains both the marker and visible demo warning."""
    return DEMO_BANNER_ATTRIBUTE in html and DEMO_BANNER_TEXT in html


def contains_demo_banner(html: str) -> bool:
    """Return whether HTML contains any part of the demo warning marker or text."""
    return DEMO_BANNER_ATTRIBUTE in html or DEMO_BANNER_TEXT in html


class Warning(BaseModel):
    """Structured stage warning item."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(default="warning", min_length=1)
    message: str = Field(min_length=1)
    path: str | None = None


class StageInput(BaseModel):
    """Standard input contract for every stage (pass paths, never file bytes)."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    input_manifest_version: str = Field(default="1.0", min_length=1)
    paths: list[str] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        for raw in values:
            norm = raw.replace("\\", "/")
            p = Path(norm)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"Unsafe or non-relative path: {raw!r}")
        return values


class StageOutput(BaseModel):
    """Standard output contract returned by every stage."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1)
    outputs: list[str] = Field(default_factory=list)
    warnings: list[Warning | str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    checksums: dict[str, str] = Field(default_factory=dict)

    @field_validator("checksums")
    @classmethod
    def validate_checksums(cls, mapping: dict[str, str]) -> dict[str, str]:
        for key, digest in mapping.items():
            if not SHA256_RE.match(digest):
                raise ValueError(f"Invalid SHA-256 digest for {key!r}: {digest!r}")
        return mapping


class Asset(BaseModel):
    """Media asset provenance and metadata contract."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    page_url: str | None = None
    local_path: str = Field(min_length=1)
    sha256: str
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    mime: str = Field(min_length=1)
    captured_at: AwareDatetime
    rights_confirmed_for_preview: bool = False
    category: str = Field(default="uncertain", min_length=1)
    notes: str | list[str] | None = None

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256_RE.match(value):
            raise ValueError("sha256 must be a 64-character lowercase hex digest")
        return value


class Claim(BaseModel):
    """Business fact claim with mandatory source attribution and timezone-aware timestamp."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    value: Any
    source_url: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    snapshot_sha256: str
    verified: bool
    status: ClaimStatus
    last_checked_at: AwareDatetime

    @field_validator("source_text")
    @classmethod
    def validate_non_blank_source_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_text must not be empty or whitespace-only")
        return value

    @field_validator("snapshot_sha256")
    @classmethod
    def validate_snapshot_sha256(cls, value: str) -> str:
        if not SHA256_RE.match(value):
            raise ValueError("snapshot_sha256 must be a 64-character lowercase hex digest")
        return value


__all__ = [
    "Asset",
    "Claim",
    "ClaimStatus",
    "DEMO_BANNER_ATTRIBUTE",
    "DEMO_BANNER_TEXT",
    "DemoKind",
    "DemoMetadata",
    "StageInput",
    "StageOutput",
    "Warning",
    "contains_demo_banner",
    "freeze_demo_metadata",
    "has_demo_banner",
    "read_demo_metadata",
]
