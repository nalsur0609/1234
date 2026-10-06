#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = Path(__file__).with_name("index.html")

BASE_REF = "ee7ecee7b7914fd088565f25b61c900d7ce37377"
DATA_REF = "6ffbef9b38f3d79bcb23a5c9b3b42e451dc22e80"
RULES_REF = "38eaac6623e934174bfdc036b36d802e7d800403"
ENGINE_REF = "e72f9d8ef0ba6fb61f21b432a189ff692b8ebfd9"
NONWEB_REF = "3132e15e7651b97ebe15a27c99f71034b087ae7d"

DATA_CATALOG_PATH = "data/catalog/r1/TEST1_CATALOG_R1.jsonl"
DATA_SUFFICIENCY_PATH = "data/catalog/r1/TEST1_DATA_FACT_SUFFICIENCY_R1.json"
RULES_PATH = "rules/r1/TEST1_GROUP_CONTRACTS_R1.json"
ENGINE_PATH = "src/test1_engine/matcher.py"
SCHEMA_PATH = "schemas/TEST1_SHARED_SCHEMAS_R1.json"

EXPECTED_BLOBS = {
    (DATA_REF, DATA_CATALOG_PATH): "013feb6d60975facf050e33869898c2a8aac0936",
    (DATA_REF, DATA_SUFFICIENCY_PATH): "43c6f8aea80822252736a79b49884431a663505b",
    (RULES_REF, RULES_PATH): "32e9d0c36a1c12ead415e55cdd7150b8d889e363",
    (ENGINE_REF, ENGINE_PATH): "47b28f6fa70bb1032e82a0d987f003368edb211a",
    (BASE_REF, SCHEMA_PATH): "f385ecc8be6d4cda69b1be854027b8dcf9a099f2",
}

DATA_SNAPSHOT_ID = "TEST1_DATA_R1_FACT_MATERIALIZATION_20261005"
RULES_SNAPSHOT_ID = "TEST1_RULES_COMPACT_ETIM_R1_SEAM_REPAIR@PR5"
ENGINE_SNAPSHOT_ID = "TEST1_ENGINE_GENERIC_MATCHER_R1_v2"
AXIAL_PROFILE = "installation.tool.axial_system.r1"

MAIN_APP_RUNTIME_REFERENCE = {
    "repository": "nalsur0609/PROGRAMMA_PODBORA_DEV2_SANDBOX",
    "issue": 189,
    "acceptance_comment": 5990065269,
    "pr": 190,
    "exact_head": "6d31b099aa16b31815a663143659b0936f596182",
    "copied_or_adapted": False,
    "note": "READ_ONLY_REFERENCE_ONLY; TEST1 WEB R1 uses a local Python standard-library HTTP runtime.",
}


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _git_show(ref: str, path: str) -> bytes:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"cannot materialize frozen input {ref}:{path}: "
            + proc.stderr.decode("utf-8", errors="replace")
        )
    return proc.stdout


def _git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def _json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class FrozenInputs:
    catalog: list[dict[str, Any]]
    sufficiency: dict[str, Any]
    rules: dict[str, Any]
    schema: dict[str, Any]
    matcher_module: Any


class RuntimeState:
    def __init__(self) -> None:
        self.inputs = self._load_frozen_inputs()
        self.matcher = self.inputs.matcher_module.GenericMatcher(self.inputs.schema)
        self.contracts = {c["group_id"]: c for c in self.inputs.rules["contracts"]}
        self.by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for product in self.inputs.catalog:
            self.by_group[product["group_id"]].append(product)
        for rows in self.by_group.values():
            rows.sort(key=lambda row: _utf8_key(str(row["exact_sku"])))

        self.eligible_by_profile = self._build_eligible_by_profile()
        self.profile_ids = sorted(self.eligible_by_profile, key=_utf8_key)
        if len(self.profile_ids) != 11:
            raise AssertionError(f"frozen executable profile count mismatch: {len(self.profile_ids)}")
        eligible_total = sum(len(rows) for rows in self.eligible_by_profile.values())
        if eligible_total != 367:
            raise AssertionError(f"frozen eligible SKU count mismatch: {eligible_total}")
        if self.inputs.sufficiency["totals"]["DATA_FACT_SUFFICIENT_SKU"] != 367:
            raise AssertionError("frozen DATA sufficiency authority no longer reports 367")

    def _load_frozen_inputs(self) -> FrozenInputs:
        raw: dict[tuple[str, str], bytes] = {}
        for ref_path, expected_blob in EXPECTED_BLOBS.items():
            ref, path = ref_path
            payload = _git_show(ref, path)
            actual_blob = _git_blob_sha1(payload)
            if actual_blob != expected_blob:
                raise AssertionError(
                    f"frozen blob mismatch {ref}:{path}: {actual_blob} != {expected_blob}"
                )
            raw[ref_path] = payload

        catalog = [
            json.loads(line)
            for line in raw[(DATA_REF, DATA_CATALOG_PATH)].decode("utf-8").splitlines()
            if line.strip()
        ]
        sufficiency = json.loads(raw[(DATA_REF, DATA_SUFFICIENCY_PATH)].decode("utf-8"))
        rules = json.loads(raw[(RULES_REF, RULES_PATH)].decode("utf-8"))
        schema = json.loads(raw[(BASE_REF, SCHEMA_PATH)].decode("utf-8"))

        with tempfile.TemporaryDirectory(prefix="test1_web_frozen_engine_") as td:
            matcher_path = Path(td) / "matcher.py"
            matcher_path.write_bytes(raw[(ENGINE_REF, ENGINE_PATH)])
            spec = importlib.util.spec_from_file_location("test1_web_frozen_matcher_r1", matcher_path)
            if spec is None or spec.loader is None:
                raise RuntimeError("cannot load frozen GenericMatcher")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

        if module.ENGINE_SNAPSHOT_ID != ENGINE_SNAPSHOT_ID:
            raise AssertionError(f"engine snapshot mismatch: {module.ENGINE_SNAPSHOT_ID}")
        if {row["data_snapshot_id"] for row in catalog} != {DATA_SNAPSHOT_ID}:
            raise AssertionError("catalog data_snapshot_id mismatch")
        if {c["rules_snapshot_id"] for c in rules["contracts"]} != {RULES_SNAPSHOT_ID}:
            raise AssertionError("rules_snapshot_id mismatch")

        return FrozenInputs(
            catalog=catalog,
            sufficiency=sufficiency,
            rules=rules,
            schema=schema,
            matcher_module=module,
        )

    def _build_eligible_by_profile(self) -> dict[str, list[dict[str, Any]]]:
        eligible: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for product in self.inputs.catalog:
            contract = self.contracts.get(product["group_id"])
            if contract is None:
                continue
            if not self.matcher.contract_capability(contract)["supported"]:
                continue
            mandatory = [p for p in contract["parameters"] if p["role"] == "MANDATORY"]
            if not mandatory:
                continue
            if all(
                product["facts"].get(p["key"], {}).get("state") == "KNOWN"
                for p in mandatory
            ):
                eligible[product["group_id"]].append(product)
        for rows in eligible.values():
            rows.sort(key=lambda row: _utf8_key(str(row["exact_sku"])))
        return dict(eligible)

    def authority_summary(self) -> dict[str, Any]:
        frozen_inputs = [
            {
                "component": "DATA_CATALOG",
                "ref": DATA_REF,
                "path": DATA_CATALOG_PATH,
                "git_blob_sha1": EXPECTED_BLOBS[(DATA_REF, DATA_CATALOG_PATH)],
            },
            {
                "component": "DATA_SUFFICIENCY",
                "ref": DATA_REF,
                "path": DATA_SUFFICIENCY_PATH,
                "git_blob_sha1": EXPECTED_BLOBS[(DATA_REF, DATA_SUFFICIENCY_PATH)],
            },
            {
                "component": "RULES",
                "ref": RULES_REF,
                "path": RULES_PATH,
                "git_blob_sha1": EXPECTED_BLOBS[(RULES_REF, RULES_PATH)],
            },
            {
                "component": "ENGINE",
                "ref": ENGINE_REF,
                "path": ENGINE_PATH,
                "git_blob_sha1": EXPECTED_BLOBS[(ENGINE_REF, ENGINE_PATH)],
            },
            {
                "component": "SHARED_SCHEMA",
                "ref": BASE_REF,
                "path": SCHEMA_PATH,
                "git_blob_sha1": EXPECTED_BLOBS[(BASE_REF, SCHEMA_PATH)],
            },
        ]
        return {
            "status": "TEST1_EXPERIMENTAL_FROZEN_WEB_R1",
            "issue": 12,
            "start_authority_comment": 5996634651,
            "engine_benchmark_boundary_comment": 5995371435,
            "nonweb_accepted_head": NONWEB_REF,
            "frozen_inputs": frozen_inputs,
            "snapshot_ids": {
                "data_snapshot_id": DATA_SNAPSHOT_ID,
                "rules_snapshot_id": RULES_SNAPSHOT_ID,
                "engine_snapshot_id": ENGINE_SNAPSHOT_ID,
            },
            "runtime_source_reference": MAIN_APP_RUNTIME_REFERENCE,
            "scope": {
                "catalog_exact_sku": 2775,
                "engine_semantic_compatible_population": 1255,
                "data_fact_sufficient_sku": 367,
                "executable_profiles": 11,
            },
            "known_frozen_findings": [
                {
                    "profile": AXIAL_PROFILE,
                    "sku_count": 7,
                    "behavior": "FAIL_CLOSED",
                    "reason": (
                        "Frozen candidate compatibility.size.tuple values are scalar strings while "
                        "frozen SET_CONTAINS accepts sequence candidates only."
                    ),
                }
            ],
            "benchmark_integration": "FORBIDDEN_NOT_PRESENT",
        }

    def _observed_values(self, profile: str, parameter_key: str, limit: int = 40) -> list[Any]:
        seen: dict[str, Any] = {}
        for product in self.by_group.get(profile, []):
            fact = product["facts"].get(parameter_key)
            if not fact or fact.get("state") != "KNOWN":
                continue
            value = copy.deepcopy(fact.get("value"))
            seen.setdefault(_json_key(value), value)
        return [seen[k] for k in sorted(seen, key=_utf8_key)[:limit]]

    def profiles_summary(self) -> dict[str, Any]:
        profiles = []
        for profile in self.profile_ids:
            contract = self.contracts[profile]
            params = []
            for parameter in contract["parameters"]:
                params.append(
                    {
                        "key": parameter["key"],
                        "role": parameter["role"],
                        "value_type": parameter["value_type"],
                        "canonical_unit": parameter.get("canonical_unit"),
                        "comparison_semantics": parameter["comparison_semantics"],
                        "request_missing_behavior": parameter.get("request_missing_behavior"),
                        "allowed_values": parameter.get("allowed_values"),
                        "observed_frozen_values": self._observed_values(profile, parameter["key"]),
                    }
                )
            profiles.append(
                {
                    "group_id": profile,
                    "contract_id": contract["contract_id"],
                    "etim_mapping": contract["etim_mapping"],
                    "eligible_real_sku": len(self.eligible_by_profile[profile]),
                    "parameters": params,
                    "known_fail_closed_finding": profile == AXIAL_PROFILE,
                }
            )
        return {
            "status": "OK",
            "profile_count": len(profiles),
            "eligible_real_sku": sum(p["eligible_real_sku"] for p in profiles),
            "profiles": profiles,
        }

    def build_request(self, payload: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
        profile = str(payload["profile"])
        raw_parameters = payload.get("parameters", {})
        if not isinstance(raw_parameters, Mapping):
            raise ValueError("parameters must be an object")

        known_keys = {str(p["key"]) for p in contract["parameters"]}
        extra = sorted(set(map(str, raw_parameters.keys())) - known_keys, key=_utf8_key)
        if extra:
            raise ValueError(f"unknown parameter keys for profile {profile}: {extra}")

        parameters: dict[str, Any] = {}
        for parameter in contract["parameters"]:
            key = str(parameter["key"])
            if key not in raw_parameters:
                continue
            supplied = raw_parameters[key]
            if not isinstance(supplied, Mapping) or "value" not in supplied:
                raise ValueError(
                    f"parameter {key} must be an object with explicit value and optional unit"
                )
            canonical = parameter.get("canonical_unit")
            supplied_unit = supplied.get("unit", canonical)
            parameters[key] = {
                "state": "PRESENT",
                "value": copy.deepcopy(supplied["value"]),
                "unit": supplied_unit,
                "evidence": ["TEST1_WEB_R1_STRUCTURED_USER_INPUT"],
            }

        request_id = str(payload.get("request_id") or "test1-web-r1-request")
        position_id = str(payload.get("position_id") or "1")
        raw_request_text = str(payload.get("raw_request_text") or "")
        return {
            "schema_version": "TEST1_REQUEST_FACTS_R1",
            "request_id": request_id,
            "position_id": position_id,
            "raw_request_text": raw_request_text,
            "detected_group": {
                "group_id": profile,
                "state": "KNOWN",
                "evidence": ["TEST1_WEB_R1_EXPLICIT_PROFILE_SELECTION"],
            },
            "parameters": parameters,
            "warnings": [
                "TEST1_EXPERIMENTAL_ONLY",
                "RAW_TEXT_NOT_PARSED_IN_WEB_R1",
            ],
        }

    def select(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        profile = payload.get("profile")
        if not isinstance(profile, str) or not profile:
            return HTTPStatus.UNPROCESSABLE_ENTITY, {
                "status": "TEST1_NOT_SELECTED",
                "result_class": "TEST1_NOT_SELECTED",
                "warnings": ["UNKNOWN_PROFILE_FAIL_CLOSED"],
                "reason": "profile must be an explicit known frozen group_id",
                "trace": {
                    "candidate_count_before_filtering": 0,
                    "candidate_decisions": [],
                    "surviving_candidates": [],
                },
            }
        if profile not in self.eligible_by_profile:
            return HTTPStatus.UNPROCESSABLE_ENTITY, {
                "status": "TEST1_NOT_SELECTED",
                "result_class": "TEST1_NOT_SELECTED",
                "warnings": ["UNKNOWN_OR_NONEXECUTABLE_PROFILE_FAIL_CLOSED"],
                "reason": f"profile is outside frozen executable scope: {profile}",
                "trace": {
                    "detected_group": profile,
                    "candidate_count_before_filtering": 0,
                    "candidate_decisions": [],
                    "surviving_candidates": [],
                },
            }

        contract = self.contracts[profile]
        try:
            request = self.build_request(payload, contract)
            run = self.matcher.select(self.by_group[profile], contract, request)
            decision_record = self.inputs.matcher_module.build_position_decision_record(
                request, contract, run
            )
            unsafe = self.inputs.matcher_module.count_selected_with_mandatory_contradiction(run)
        except ValueError as exc:
            return HTTPStatus.BAD_REQUEST, {
                "status": "INVALID_STRUCTURED_INPUT",
                "result_class": "TEST1_NOT_SELECTED",
                "warnings": ["STRUCTURED_INPUT_REJECTED"],
                "reason": str(exc),
            }

        return HTTPStatus.OK, {
            "status": "OK",
            "experimental": True,
            "request": request,
            "result": run.result,
            "trace": run.trace,
            "decision_record": decision_record,
            "safety": {
                "selected_with_mandatory_contradiction": unsafe,
            },
        }


class Test1WebHandler(BaseHTTPRequestHandler):
    server_version = "TEST1WebR1/1.0"

    @property
    def state(self) -> RuntimeState:
        return self.server.runtime_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[TEST1-WEB-R1] " + (fmt % args) + "\n")

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self) -> None:
        body = INDEX_PATH.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html()
            return
        if path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "TEST1-WEB-1",
                    "experimental": True,
                    "engine_snapshot_id": ENGINE_SNAPSHOT_ID,
                    "profile_count": len(self.state.profile_ids),
                },
            )
            return
        if path == "/api/authority":
            self._send_json(HTTPStatus.OK, self.state.authority_summary())
            return
        if path == "/api/profiles":
            self._send_json(HTTPStatus.OK, self.state.profiles_summary())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"status": "NOT_FOUND", "path": path})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/select":
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "NOT_FOUND", "path": path})
            return
        length = self.headers.get("Content-Length")
        try:
            size = int(length or "0")
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "BAD_CONTENT_LENGTH"})
            return
        if size <= 0 or size > 2_000_000:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "INVALID_BODY_SIZE"})
            return
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "INVALID_JSON"})
            return
        if not isinstance(payload, Mapping):
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "JSON_OBJECT_REQUIRED"})
            return
        status, response = self.state.select(payload)
        self._send_json(status, response)


class Test1WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], runtime_state: RuntimeState):
        self.runtime_state = runtime_state
        super().__init__(server_address, Test1WebHandler)


def create_server(host: str, port: int) -> Test1WebServer:
    state = RuntimeState()
    return Test1WebServer((host, port), state)


def serve(host: str, port: int) -> None:
    server = create_server(host, port)
    actual_host, actual_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "status": "TEST1_WEB_R1_LISTENING",
                "host": actual_host,
                "port": actual_port,
                "profiles": len(server.runtime_state.profile_ids),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="TEST1 frozen local web runtime R1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
