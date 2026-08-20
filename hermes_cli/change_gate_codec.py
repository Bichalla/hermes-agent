"""Strict codecs for default-off Change Gate runtime artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, TypeAlias, cast

from hermes_cli.change_gate import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    DURABLE_RELEASE_SCHEMA,
    EVIDENCE_PACKET_SCHEMA,
    FROZEN_HANDOFF_SCHEMA,
    MAX_RUNTIME_ARTIFACT_BYTES,
    RELEASE_RECEIPT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    ArchitectureInventoryRecord,
    ArtifactBinding,
    DurableReleaseArtifact,
    EvidencePacket,
    FrozenHandoff,
    HumanReleaseReceipt,
    ReleasePurpose,
    ReviewResult,
    ReviewRoute,
    ReviewVerdict,
    ReviewerClass,
    RiskLevel,
    RouteProjection,
    SourceIdentity,
    UpstreamRouteSelector,
    WorkIdentity,
    canonical_json_bytes,
)

ChangeGateArtifact: TypeAlias = (
    EvidencePacket | FrozenHandoff | ArchitectureInventoryRecord | ReviewResult | DurableReleaseArtifact
)

_Schema: TypeAlias = Literal[
    "hermes.change-gate.evidence-packet/v1",
    "hermes.change-gate.frozen-handoff/v1",
    "hermes.change-gate.architecture-inventory/v1",
    "hermes.change-gate.review-result/v1",
    "hermes.change-gate.durable-release/v1",
]

_SUPPORTED_SCHEMAS: Final[set[str]] = {
    EVIDENCE_PACKET_SCHEMA,
    FROZEN_HANDOFF_SCHEMA,
    ARCHITECTURE_INVENTORY_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    DURABLE_RELEASE_SCHEMA,
}


class ArtifactCodecReason(StrEnum):
    OK = "ok"
    OVERSIZED = "oversized"
    INVALID_UTF8 = "invalid_utf8"
    DUPLICATE_KEY = "duplicate_key"
    INVALID_JSON = "invalid_json"
    FLOAT_REJECTED = "float_rejected"
    NON_CANONICAL = "non_canonical"
    ROOT_NOT_OBJECT = "root_not_object"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    WRONG_SCHEMA = "wrong_schema"
    UNKNOWN_FIELD = "unknown_field"
    MISSING_FIELD = "missing_field"
    TYPE_INVALID = "type_invalid"
    VALUE_INVALID = "value_invalid"
    DIGEST_MISMATCH = "digest_mismatch"
    PATH_INVALID = "path_invalid"
    PATH_TRAVERSAL = "path_traversal"
    FILE_NOT_REGULAR = "file_not_regular"
    FILE_READ_FAILED = "file_read_failed"
    ARTIFACT_BINDING_INVALID = "artifact_binding_invalid"


@dataclass(frozen=True, slots=True)
class ArtifactCodecResult:
    value: ChangeGateArtifact | None
    reason: ArtifactCodecReason
    sha256: str | None = None
    byte_count: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is ArtifactCodecReason.OK and self.value is not None

    @property
    def artifact(self) -> ChangeGateArtifact | None:
        return self.value


def encode_artifact(artifact: ChangeGateArtifact) -> bytes:
    """Return canonical UTF-8 JSON bytes for one supported artifact."""

    if not _supported_artifact(artifact):
        raise TypeError("unsupported_change_gate_artifact")
    return canonical_json_bytes(artifact)


def decode_artifact(
    data: bytes,
    *,
    expected_schema: str,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_RUNTIME_ARTIFACT_BYTES,
) -> ArtifactCodecResult:
    byte_count = len(data)
    if byte_count > max_bytes:
        return ArtifactCodecResult(None, ArtifactCodecReason.OVERSIZED, byte_count=byte_count)
    if expected_sha256 is not None and not _is_sha256(expected_sha256):
        return ArtifactCodecResult(None, ArtifactCodecReason.DIGEST_MISMATCH, byte_count=byte_count)

    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        return ArtifactCodecResult(None, ArtifactCodecReason.DIGEST_MISMATCH, digest, byte_count)

    decoded = _load_json_object(data)
    if isinstance(decoded, ArtifactCodecReason):
        return ArtifactCodecResult(None, decoded, digest, byte_count)
    schema = decoded.get("schema")
    if type(schema) is not str or schema not in _SUPPORTED_SCHEMAS:
        return ArtifactCodecResult(None, ArtifactCodecReason.UNSUPPORTED_SCHEMA, digest, byte_count)
    if schema != expected_schema:
        return ArtifactCodecResult(None, ArtifactCodecReason.WRONG_SCHEMA, digest, byte_count)

    try:
        artifact = _artifact_from_object(decoded)
        canonical = canonical_json_bytes(artifact)
    except _CodecError as exc:
        return ArtifactCodecResult(None, exc.reason, digest, byte_count)
    except (TypeError, ValueError):
        return ArtifactCodecResult(None, ArtifactCodecReason.VALUE_INVALID, digest, byte_count)
    if canonical != data:
        return ArtifactCodecResult(None, ArtifactCodecReason.NON_CANONICAL, digest, byte_count)
    return ArtifactCodecResult(artifact, ArtifactCodecReason.OK, digest, byte_count)


def read_artifact(
    owner_root: Path,
    relative_path: str,
    *,
    expected_schema: str,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_RUNTIME_ARTIFACT_BYTES,
) -> ArtifactCodecResult:
    path_result = _resolve_owner_relative_file(owner_root, relative_path)
    if isinstance(path_result, ArtifactCodecReason):
        return ArtifactCodecResult(None, path_result)
    try:
        data = path_result.read_bytes()
    except OSError:
        return ArtifactCodecResult(None, ArtifactCodecReason.FILE_READ_FAILED)
    return decode_artifact(
        data,
        expected_schema=expected_schema,
        expected_sha256=expected_sha256,
        max_bytes=max_bytes,
    )


def validate_artifact_binding(
    binding: ArtifactBinding,
    *,
    expected_role: str | None = None,
    require_git_oid: bool = False,
) -> ArtifactCodecReason:
    if type(binding) is not ArtifactBinding:
        return ArtifactCodecReason.ARTIFACT_BINDING_INVALID
    if not _valid_relative_path(binding.path):
        return ArtifactCodecReason.PATH_INVALID
    if not _is_sha256(binding.sha256):
        return ArtifactCodecReason.ARTIFACT_BINDING_INVALID
    if not _valid_text(binding.role):
        return ArtifactCodecReason.ARTIFACT_BINDING_INVALID
    if expected_role is not None and binding.role != expected_role:
        return ArtifactCodecReason.ARTIFACT_BINDING_INVALID
    if require_git_oid and binding.git_oid is None:
        return ArtifactCodecReason.ARTIFACT_BINDING_INVALID
    if binding.git_oid is not None and not _is_git_oid(binding.git_oid):
        return ArtifactCodecReason.ARTIFACT_BINDING_INVALID
    return ArtifactCodecReason.OK


class _CodecError(ValueError):
    def __init__(self, reason: ArtifactCodecReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _load_json_object(data: bytes) -> dict[str, Any] | ArtifactCodecReason:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ArtifactCodecReason.INVALID_UTF8

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise _CodecError(ArtifactCodecReason.DUPLICATE_KEY)
            out[key] = value
        return out

    def reject_float(_: str) -> None:
        raise _CodecError(ArtifactCodecReason.FLOAT_REJECTED)

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_float=reject_float,
            parse_constant=reject_float,
        )
    except _CodecError as exc:
        return exc.reason
    except json.JSONDecodeError:
        return ArtifactCodecReason.INVALID_JSON
    return value if type(value) is dict else ArtifactCodecReason.ROOT_NOT_OBJECT


def _artifact_from_object(value: dict[str, Any]) -> ChangeGateArtifact:
    schema = value["schema"]
    if schema == EVIDENCE_PACKET_SCHEMA:
        return _evidence(value)
    if schema == FROZEN_HANDOFF_SCHEMA:
        return _handoff(value)
    if schema == ARCHITECTURE_INVENTORY_SCHEMA:
        return _inventory(value)
    if schema == REVIEW_RESULT_SCHEMA:
        return _review(value)
    if schema == DURABLE_RELEASE_SCHEMA:
        return _durable_release(value)
    raise _CodecError(ArtifactCodecReason.UNSUPPORTED_SCHEMA)


def _source(value: object) -> SourceIdentity:
    data = _object(value, {"repository", "branch", "commit", "tree"})
    repository = _text(data["repository"])
    branch = _text(data["branch"])
    commit = _git_oid(data["commit"])
    tree = _git_oid(data["tree"])
    return SourceIdentity(repository=repository, branch=branch, commit=commit, tree=tree)


def _work(value: object) -> WorkIdentity:
    data = _object(value, {"task_id", "run_id", "work_id", "operation", "effect"})
    return WorkIdentity(
        task_id=_text(data["task_id"]),
        run_id=_text(data["run_id"]),
        work_id=_text(data["work_id"]),
        operation=_text(data["operation"]),
        effect=_text(data["effect"]),
    )


def _artifact_binding(value: object) -> ArtifactBinding:
    data = _object(value, {"path", "sha256", "role", "git_oid"})
    git_oid_raw = data["git_oid"]
    if git_oid_raw is not None:
        git_oid = _git_oid(git_oid_raw)
    else:
        git_oid = None
    binding = ArtifactBinding(
        path=_relative_path(data["path"]),
        sha256=_sha256(data["sha256"]),
        role=_text(data["role"]),
        git_oid=git_oid,
    )
    if validate_artifact_binding(binding) is not ArtifactCodecReason.OK:
        raise _CodecError(ArtifactCodecReason.ARTIFACT_BINDING_INVALID)
    return binding


def _selector(value: object) -> UpstreamRouteSelector:
    data = _object(value, {"assignee", "model_override", "provider_override", "reasoning_effort"})
    model = _optional_text(data["model_override"])
    provider = _optional_text(data["provider_override"])
    effort = _optional_text(data["reasoning_effort"])
    if provider is not None and model is None:
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return UpstreamRouteSelector(
        assignee=_text(data["assignee"]),
        model_override=model,
        provider_override=provider,
        reasoning_effort=effort,
    )


def _review_route(value: object) -> ReviewRoute:
    data = _object(value, {"reviewer_class", "selector"})
    return ReviewRoute(
        reviewer_class=_reviewer_class(data["reviewer_class"]),
        selector=_selector(data["selector"]),
    )


def _route(value: object) -> RouteProjection:
    data = _object(value, {"risk", "executor", "reviews"})
    risk = _risk(data["risk"])
    reviews = _tuple(data["reviews"], _review_route)
    reviewer_classes = tuple(route.reviewer_class for route in reviews)
    if risk in {RiskLevel.LOW, RiskLevel.NORMAL} and reviewer_classes != (ReviewerClass.REVIEWER,):
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    if risk is RiskLevel.HIGH and reviewer_classes != (ReviewerClass.NORMAL, ReviewerClass.DEEP):
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return RouteProjection(risk=risk, executor=_selector(data["executor"]), reviews=reviews)


def _inventory(value: dict[str, Any]) -> ArchitectureInventoryRecord:
    data = _closed(
        value,
        {
            "schema",
            "inventory_id",
            "capability",
            "owner",
            "consumers",
            "authority_contract",
            "activation_state",
            "source_paths",
            "artifact_paths",
            "risk",
            "blast_radius",
        },
    )
    if data["schema"] != ARCHITECTURE_INVENTORY_SCHEMA:
        raise _CodecError(ArtifactCodecReason.WRONG_SCHEMA)
    return ArchitectureInventoryRecord(
        schema=ARCHITECTURE_INVENTORY_SCHEMA,
        inventory_id=_text(data["inventory_id"]),
        capability=_text(data["capability"]),
        owner=_text(data["owner"]),
        consumers=_sorted_text_tuple(data["consumers"], allow_empty=False),
        authority_contract=_text(data["authority_contract"]),
        activation_state=_text(data["activation_state"]),
        source_paths=_sorted_path_tuple(data["source_paths"], allow_empty=False),
        artifact_paths=_sorted_path_tuple(data["artifact_paths"], allow_empty=False),
        risk=_risk(data["risk"]),
        blast_radius=_sorted_text_tuple(data["blast_radius"], allow_empty=False),
    )


def _evidence(value: dict[str, Any]) -> EvidencePacket:
    data = _closed(
        value,
        {
            "source",
            "work",
            "risk",
            "route",
            "allowed_paths",
            "required_inputs",
            "produced_artifacts",
            "inventory_id",
            "created_at_epoch",
            "expires_at_epoch",
            "schema",
        },
    )
    if data["schema"] != EVIDENCE_PACKET_SCHEMA:
        raise _CodecError(ArtifactCodecReason.WRONG_SCHEMA)
    risk = _risk(data["risk"])
    route = _route(data["route"])
    if route.risk is not risk:
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return EvidencePacket(
        source=_source(data["source"]),
        work=_work(data["work"]),
        risk=risk,
        route=route,
        allowed_paths=_sorted_path_tuple(data["allowed_paths"], allow_empty=False),
        required_inputs=_sorted_artifact_tuple(data["required_inputs"]),
        produced_artifacts=_sorted_artifact_tuple(data["produced_artifacts"]),
        inventory_id=_text(data["inventory_id"]),
        created_at_epoch=_nonnegative_int(data["created_at_epoch"]),
        expires_at_epoch=_positive_int(data["expires_at_epoch"]),
        schema=EVIDENCE_PACKET_SCHEMA,
    )


def _handoff(value: dict[str, Any]) -> FrozenHandoff:
    data = _closed(
        value,
        {
            "evidence_sha256",
            "source",
            "work",
            "allowed_paths",
            "required_inputs",
            "produced_artifacts",
            "route",
            "scope",
            "forbidden_effects",
            "inventory_id",
            "inventory_sha256",
            "inventory_owner",
            "inventory_consumer",
            "convergence_rule",
            "claim_release_required",
            "g4_release_required",
            "schema",
        },
    )
    if data["schema"] != FROZEN_HANDOFF_SCHEMA:
        raise _CodecError(ArtifactCodecReason.WRONG_SCHEMA)
    if data["claim_release_required"] is not True or data["g4_release_required"] is not True:
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return FrozenHandoff(
        evidence_sha256=_sha256(data["evidence_sha256"]),
        source=_source(data["source"]),
        work=_work(data["work"]),
        allowed_paths=_sorted_path_tuple(data["allowed_paths"], allow_empty=False),
        required_inputs=_sorted_artifact_tuple(data["required_inputs"]),
        produced_artifacts=_sorted_artifact_tuple(data["produced_artifacts"]),
        route=_route(data["route"]),
        scope=_sorted_text_tuple(data["scope"], allow_empty=False),
        forbidden_effects=_sorted_text_tuple(data["forbidden_effects"], allow_empty=False),
        inventory_id=_text(data["inventory_id"]),
        inventory_sha256=_sha256(data["inventory_sha256"]),
        inventory_owner=_text(data["inventory_owner"]),
        inventory_consumer=_text(data["inventory_consumer"]),
        convergence_rule=_text(data["convergence_rule"]),
        claim_release_required=True,
        g4_release_required=True,
        schema=FROZEN_HANDOFF_SCHEMA,
    )


def _review(value: dict[str, Any]) -> ReviewResult:
    data = _closed(
        value,
        {
            "bundle_sha256",
            "reviewer_class",
            "reviewer_identity",
            "attempt_id",
            "verdict",
            "finding_codes",
            "completed_at_epoch",
            "schema",
        },
    )
    if data["schema"] != REVIEW_RESULT_SCHEMA:
        raise _CodecError(ArtifactCodecReason.WRONG_SCHEMA)
    verdict = _review_verdict(data["verdict"])
    finding_codes = _sorted_text_tuple(data["finding_codes"], allow_empty=True)
    if verdict is ReviewVerdict.PASS and finding_codes:
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    if verdict is not ReviewVerdict.PASS and not finding_codes:
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return ReviewResult(
        bundle_sha256=_sha256(data["bundle_sha256"]),
        reviewer_class=_reviewer_class(data["reviewer_class"]),
        reviewer_identity=_text(data["reviewer_identity"]),
        attempt_id=_text(data["attempt_id"]),
        verdict=verdict,
        finding_codes=finding_codes,
        completed_at_epoch=_nonnegative_int(data["completed_at_epoch"]),
        schema=REVIEW_RESULT_SCHEMA,
    )


def _human_release_receipt(value: object) -> HumanReleaseReceipt:
    data = _object(
        value,
        {
            "purpose",
            "handoff_sha256",
            "action_fingerprint",
            "turn_id_sha256",
            "session_scope_sha256",
            "platform_scope_sha256",
            "user_message_index",
            "source_role",
            "schema",
        },
    )
    if data["schema"] != RELEASE_RECEIPT_SCHEMA:
        raise _CodecError(ArtifactCodecReason.WRONG_SCHEMA)
    return HumanReleaseReceipt(
        purpose=_release_purpose(data["purpose"]),
        handoff_sha256=_sha256(data["handoff_sha256"]),
        action_fingerprint=_sha256(data["action_fingerprint"]),
        turn_id_sha256=_sha256(data["turn_id_sha256"]),
        session_scope_sha256=_sha256(data["session_scope_sha256"]),
        platform_scope_sha256=_sha256(data["platform_scope_sha256"]),
        user_message_index=_nonnegative_int(data["user_message_index"]),
        source_role=_text(data["source_role"]),
        schema=RELEASE_RECEIPT_SCHEMA,
    )


def _durable_release(value: dict[str, Any]) -> DurableReleaseArtifact:
    data = _closed(
        value,
        {
            "release_id",
            "purpose",
            "handoff_sha256",
            "evidence_sha256",
            "inventory_sha256",
            "artifact_set_sha256",
            "route_sha256",
            "task_id",
            "work_id",
            "source",
            "authority_receipt",
            "issued_at_epoch",
            "expires_at_epoch",
            "max_consumptions",
            "schema",
        },
    )
    if data["schema"] != DURABLE_RELEASE_SCHEMA:
        raise _CodecError(ArtifactCodecReason.WRONG_SCHEMA)
    artifact = DurableReleaseArtifact(
        release_id=_text(data["release_id"]),
        purpose=_release_purpose(data["purpose"]),
        handoff_sha256=_sha256(data["handoff_sha256"]),
        evidence_sha256=_sha256(data["evidence_sha256"]),
        inventory_sha256=_sha256(data["inventory_sha256"]),
        artifact_set_sha256=_sha256(data["artifact_set_sha256"]),
        route_sha256=_sha256(data["route_sha256"]),
        task_id=_text(data["task_id"]),
        work_id=_text(data["work_id"]),
        source=_source(data["source"]),
        authority_receipt=_human_release_receipt(data["authority_receipt"]),
        issued_at_epoch=_nonnegative_int(data["issued_at_epoch"]),
        expires_at_epoch=_positive_int(data["expires_at_epoch"]),
        max_consumptions=_positive_int(data["max_consumptions"]),
        schema=DURABLE_RELEASE_SCHEMA,
    )
    if artifact.max_consumptions != 1:
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    if artifact.authority_receipt.purpose is not artifact.purpose:
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    if artifact.authority_receipt.handoff_sha256 != artifact.handoff_sha256:
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return artifact


_EVIDENCE_KEYS: Final[set[str]] = {
    "source",
    "work",
    "risk",
    "route",
    "allowed_paths",
    "required_inputs",
    "produced_artifacts",
    "inventory_id",
    "created_at_epoch",
    "expires_at_epoch",
    "schema",
}

_HANDOFF_KEYS: Final[set[str]] = {
    "evidence_sha256",
    "source",
    "work",
    "allowed_paths",
    "required_inputs",
    "produced_artifacts",
    "route",
    "scope",
    "forbidden_effects",
    "inventory_id",
    "inventory_sha256",
    "inventory_owner",
    "inventory_consumer",
    "convergence_rule",
    "claim_release_required",
    "g4_release_required",
    "schema",
}


def _object(value: object, expected_keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    return _closed(cast(dict[str, Any], value), expected_keys)


def _closed(value: dict[str, Any], expected_keys: set[str]) -> dict[str, Any]:
    keys = set(value)
    if keys - expected_keys:
        raise _CodecError(ArtifactCodecReason.UNKNOWN_FIELD)
    if expected_keys - keys:
        raise _CodecError(ArtifactCodecReason.MISSING_FIELD)
    return value


def _tuple(value: object, item_parser: Any) -> tuple[Any, ...]:
    if type(value) is not list:
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    return tuple(item_parser(item) for item in value)


def _sorted_artifact_tuple(value: object) -> tuple[ArtifactBinding, ...]:
    bindings = _tuple(value, _artifact_binding)
    keys = tuple((item.role, item.path, item.sha256, item.git_oid or "") for item in bindings)
    if len(keys) != len(set(keys)) or keys != tuple(sorted(keys)):
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return bindings


def _sorted_text_tuple(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    values = _tuple(value, _text)
    if (not allow_empty and not values) or len(values) != len(set(values)) or values != tuple(sorted(values)):
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return values


def _sorted_path_tuple(value: object, *, allow_empty: bool) -> tuple[str, ...]:
    values = _tuple(value, _relative_path)
    if (not allow_empty and not values) or len(values) != len(set(values)) or values != tuple(sorted(values)):
        raise _CodecError(ArtifactCodecReason.VALUE_INVALID)
    return values


def _text(value: object) -> str:
    if not _valid_text(value):
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    assert type(value) is str
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _relative_path(value: object) -> str:
    path = _text(value)
    if not _valid_relative_path(path):
        raise _CodecError(ArtifactCodecReason.PATH_INVALID)
    return path


def _sha256(value: object) -> str:
    if not _is_sha256(value):
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    assert type(value) is str
    return value


def _git_oid(value: object) -> str:
    if not _is_git_oid(value):
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    assert type(value) is str
    return value


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    return value


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    return value


def _risk(value: object) -> RiskLevel:
    if type(value) is not str:
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    return RiskLevel(value)


def _reviewer_class(value: object) -> ReviewerClass:
    if type(value) is not str:
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    return ReviewerClass(value)


def _review_verdict(value: object) -> ReviewVerdict:
    if type(value) is not str:
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    return ReviewVerdict(value)


def _release_purpose(value: object) -> ReleasePurpose:
    if type(value) is not str:
        raise _CodecError(ArtifactCodecReason.TYPE_INVALID)
    return ReleasePurpose(value)


def _resolve_owner_relative_file(owner_root: Path, relative_path: str) -> Path | ArtifactCodecReason:
    if not _valid_relative_path(relative_path):
        return ArtifactCodecReason.PATH_TRAVERSAL
    try:
        root = owner_root.resolve(strict=True)
    except OSError:
        return ArtifactCodecReason.FILE_READ_FAILED
    candidate = root.joinpath(relative_path)
    try:
        cursor = root
        for part in PurePosixPath(relative_path).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return ArtifactCodecReason.FILE_NOT_REGULAR
    except OSError:
        return ArtifactCodecReason.FILE_READ_FAILED
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return ArtifactCodecReason.FILE_READ_FAILED
    try:
        resolved.relative_to(root)
    except ValueError:
        return ArtifactCodecReason.PATH_TRAVERSAL
    try:
        if not resolved.is_file():
            return ArtifactCodecReason.FILE_NOT_REGULAR
    except OSError:
        return ArtifactCodecReason.FILE_READ_FAILED
    return resolved


def _valid_relative_path(path: object) -> bool:
    if not _valid_text(path):
        return False
    assert isinstance(path, str)
    if "\\" in path:
        return False
    candidate = PurePosixPath(path)
    return (
        not candidate.is_absolute()
        and path == candidate.as_posix()
        and path not in {".", ".."}
        and ".." not in candidate.parts
    )


def _valid_text(value: object) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and "\0" not in value


def _is_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _is_git_oid(value: object) -> bool:
    return (
        type(value) is str
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
    )


def _supported_artifact(artifact: object) -> bool:
    return type(artifact) in {
        EvidencePacket,
        FrozenHandoff,
        ArchitectureInventoryRecord,
        ReviewResult,
        DurableReleaseArtifact,
    }


def encode_change_gate_artifact(artifact: ChangeGateArtifact) -> bytes:
    return encode_artifact(artifact)


def decode_change_gate_artifact(
    data: bytes,
    *,
    expected_schema: str,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_RUNTIME_ARTIFACT_BYTES,
) -> ArtifactCodecResult:
    return decode_artifact(
        data,
        expected_schema=expected_schema,
        expected_sha256=expected_sha256,
        max_bytes=max_bytes,
    )


def read_change_gate_artifact(
    owner_root: Path,
    relative_path: str,
    *,
    expected_schema: str,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_RUNTIME_ARTIFACT_BYTES,
) -> ArtifactCodecResult:
    return read_artifact(
        owner_root,
        relative_path,
        expected_schema=expected_schema,
        expected_sha256=expected_sha256,
        max_bytes=max_bytes,
    )


ChangeGateCodecReason = ArtifactCodecReason
ChangeGateCodecResult = ArtifactCodecResult


__all__ = [
    "ArtifactCodecReason",
    "ArtifactCodecResult",
    "ChangeGateArtifact",
    "ChangeGateCodecReason",
    "ChangeGateCodecResult",
    "decode_artifact",
    "decode_change_gate_artifact",
    "encode_artifact",
    "encode_change_gate_artifact",
    "read_artifact",
    "read_change_gate_artifact",
    "validate_artifact_binding",
]
