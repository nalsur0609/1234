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
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SUMMARY_PATH = ROOT / "integration/r1/TEST1_NONWEB_INTEGRATION_R1.json"
RESULTS_PATH = ROOT / "integration/r1/TEST1_NONWEB_INTEGRATION_RESULTS_R1.jsonl"

BASE_REF = "ee7ecee7b7914fd088565f25b61c900d7ce37377"
DATA_REF = "6ffbef9b38f3d79bcb23a5c9b3b42e451dc22e80"
RULES_REF = "38eaac6623e934174bfdc036b36d802e7d800403"
ENGINE_REF = "e72f9d8ef0ba6fb61f21b432a189ff692b8ebfd9"

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

NOT_EXECUTED_REASON = (
    "No safe source-backed alternate mandatory value and no numeric accepted primitive "
    "can produce an explicit contradiction for this profile without inventing opaque semantics."
)


def utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def git_show(ref: str, path: str) -> bytes:
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


def git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def load_frozen_inputs() -> dict[str, Any]:
    raw: dict[tuple[str, str], bytes] = {}
    for ref_path, expected in EXPECTED_BLOBS.items():
        ref, path = ref_path
        payload = git_show(ref, path)
        actual = git_blob_sha1(payload)
        if actual != expected:
            raise AssertionError(f"blob mismatch {ref}:{path}: {actual} != {expected}")
        raw[ref_path] = payload

    catalog = [
        json.loads(line)
        for line in raw[(DATA_REF, DATA_CATALOG_PATH)].decode("utf-8").splitlines()
        if line
    ]
    sufficiency = json.loads(raw[(DATA_REF, DATA_SUFFICIENCY_PATH)].decode("utf-8"))
    rules = json.loads(raw[(RULES_REF, RULES_PATH)].decode("utf-8"))
    schema = json.loads(raw[(BASE_REF, SCHEMA_PATH)].decode("utf-8"))

    with tempfile.TemporaryDirectory(prefix="test1_frozen_engine_") as td:
        matcher_path = Path(td) / "matcher.py"
        matcher_path.write_bytes(raw[(ENGINE_REF, ENGINE_PATH)])
        spec = importlib.util.spec_from_file_location("test1_frozen_matcher_r1", matcher_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load frozen matcher module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

    if module.ENGINE_SNAPSHOT_ID != ENGINE_SNAPSHOT_ID:
        raise AssertionError(f"engine snapshot mismatch: {module.ENGINE_SNAPSHOT_ID}")
    return {
        "catalog": catalog,
        "sufficiency": sufficiency,
        "rules": rules,
        "schema": schema,
        "engine_module": module,
    }


def request_fact_from_candidate(fact: dict[str, Any]) -> dict[str, Any]:
    refs = list(fact.get("source_refs") or [])
    if not refs:
        raise AssertionError("positive fixture mandatory fact has no source_refs")
    return {
        "state": "PRESENT",
        "value": copy.deepcopy(fact.get("value")),
        "unit": fact.get("unit"),
        "evidence": refs,
    }


def build_request(
    *,
    fixture_id: str,
    profile: str,
    anchor: dict[str, Any],
    contract: dict[str, Any],
    warning: str,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for parameter in contract["parameters"]:
        if parameter["role"] != "MANDATORY":
            continue
        fact = anchor["facts"][parameter["key"]]
        if fact["state"] != "KNOWN":
            raise AssertionError(f"{anchor['exact_sku']}:{parameter['key']} is not KNOWN")
        parameters[parameter["key"]] = request_fact_from_candidate(fact)
    return {
        "schema_version": "TEST1_REQUEST_FACTS_R1",
        "request_id": fixture_id,
        "position_id": fixture_id,
        "raw_request_text": f"TEST1 frozen integration fixture from exact SKU {anchor['exact_sku']}",
        "detected_group": {
            "group_id": profile,
            "state": "KNOWN",
            "evidence": [f"frozen-data:{DATA_REF}:{anchor['exact_sku']}"],
        },
        "parameters": parameters,
        "warnings": [warning],
    }


def numeric_contradiction_value(semantic: str, values: list[float | int]) -> float | int | None:
    if not values:
        return None
    if semantic == "NUMERIC_EQUAL":
        return max(values) + 1
    if semantic == "CANDIDATE_LTE_REQUEST":
        return min(values) - 1
    if semantic == "CANDIDATE_GTE_REQUEST":
        return max(values) + 1
    return None


def candidate_has_mandatory_contradiction(run: Any, sku: str, key: str | None = None) -> bool:
    for decision in run.trace["candidate_decisions"]:
        if decision["exact_sku"] != sku:
            continue
        return any(
            comparison["role"] == "MANDATORY"
            and comparison["outcome"] == "CONTRADICTION"
            and (key is None or comparison["key"] == key)
            for comparison in decision["comparisons"]
        )
    return False


def tie_count(run: Any) -> int:
    selected = run.result["selected_exact_sku"]
    if selected is None:
        return 0
    selected_decision = next(
        decision for decision in run.trace["candidate_decisions"]
        if decision["exact_sku"] == selected
    )
    return sum(
        1
        for decision in run.trace["candidate_decisions"]
        if not decision["excluded"]
        and decision["probable_eligible"]
        and decision["exact_eligible"] == selected_decision["exact_eligible"]
        and float(decision["total_score"]) == float(selected_decision["total_score"])
    )


def project_run(spec: dict[str, Any], run: Any, engine_module: Any) -> dict[str, Any]:
    selected_bad = bool(engine_module.count_selected_with_mandatory_contradiction(run))
    record = {
        "fixture_id": spec["fixture_id"],
        "execution_status": "EXECUTED",
        "fixture_kind": spec["fixture_kind"],
        "profile": spec["profile"],
        "source_sku": spec["source_sku"],
        "mutated_parameter": spec.get("mutated_parameter"),
        "mutation_method": spec.get("mutation_method"),
        "request": spec["request"],
        "result_class": run.result["result_class"],
        "selected_exact_sku": run.result["selected_exact_sku"],
        "candidate_count_before_filtering": run.trace["candidate_count_before_filtering"],
        "surviving_candidates": list(run.trace["surviving_candidates"]),
        "tie_candidate_count_before_sku_tiebreak": tie_count(run),
        "selected_with_mandatory_contradiction": selected_bad,
        "anchor_candidate_mandatory_contradiction": candidate_has_mandatory_contradiction(
            run, spec["source_sku"], spec.get("mutated_parameter")
        ),
        "engine_decision_trace_ref": run.result["decision_trace_ref"],
        "warnings": list(run.result["warnings"]),
        "snapshot_ids": {
            "data_snapshot_id": run.result["data_snapshot_id"],
            "rules_snapshot_id": run.result["rules_snapshot_id"],
            "engine_snapshot_id": run.result["engine_snapshot_id"],
        },
    }
    return record


def build_fixture_specs(inputs: dict[str, Any], matcher: Any) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    catalog = inputs["catalog"]
    contracts = {c["group_id"]: c for c in inputs["rules"]["contracts"]}
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in catalog:
        by_group[product["group_id"]].append(product)
    for rows in by_group.values():
        rows.sort(key=lambda r: utf8_key(str(r["exact_sku"])))

    eligible: list[dict[str, Any]] = []
    for product in catalog:
        contract = contracts.get(product["group_id"])
        if contract is None or not matcher.contract_capability(contract)["supported"]:
            continue
        mandatory = [p for p in contract["parameters"] if p["role"] == "MANDATORY"]
        if mandatory and all(product["facts"].get(p["key"], {}).get("state") == "KNOWN" for p in mandatory):
            eligible.append(product)
    eligible.sort(key=lambda r: (utf8_key(r["group_id"]), utf8_key(r["exact_sku"])))

    profiles = sorted({r["group_id"] for r in eligible}, key=utf8_key)
    if len(eligible) != 367 or len(profiles) != 11:
        raise AssertionError(f"frozen cohort mismatch: eligible={len(eligible)} profiles={len(profiles)}")
    if inputs["sufficiency"]["totals"]["DATA_FACT_SUFFICIENT_SKU"] != 367:
        raise AssertionError("DATA sufficiency authority does not report 367")
    if {r["data_snapshot_id"] for r in catalog} != {DATA_SNAPSHOT_ID}:
        raise AssertionError("data_snapshot_id mismatch")
    if {c["rules_snapshot_id"] for c in contracts.values()} != {RULES_SNAPSHOT_ID}:
        raise AssertionError("rules_snapshot_id mismatch")

    for profile in profiles:
        if any(p["role"] != "MANDATORY" for p in contracts[profile]["parameters"]):
            raise AssertionError(
                f"{profile}: R1 compact trace projection assumes the frozen cohort contract is all-MANDATORY"
            )

    specs: list[dict[str, Any]] = []
    for index, anchor in enumerate(eligible, 1):
        profile = anchor["group_id"]
        fixture_id = f"positive:{index:04d}"
        request = build_request(
            fixture_id=fixture_id,
            profile=profile,
            anchor=anchor,
            contract=contracts[profile],
            warning="MECHANICAL_INTEGRATION_FIXTURE_NOT_EXPERT_QA",
        )
        specs.append({
            "fixture_id": fixture_id,
            "execution_status": "EXECUTED",
            "fixture_kind": "POSITIVE",
            "profile": profile,
            "source_sku": anchor["exact_sku"],
            "request": request,
        })

    eligible_by_profile = {
        profile: sorted(
            [row for row in eligible if row["group_id"] == profile],
            key=lambda r: utf8_key(r["exact_sku"]),
        )
        for profile in profiles
    }

    for index, profile in enumerate(profiles, 1):
        anchor = eligible_by_profile[profile][0]
        contract = contracts[profile]
        first_mandatory = next(p for p in contract["parameters"] if p["role"] == "MANDATORY")
        fixture_id = f"missing:{index:02d}"
        request = build_request(
            fixture_id=fixture_id,
            profile=profile,
            anchor=anchor,
            contract=contract,
            warning="NEGATIVE_MISSING_MANDATORY_FIXTURE",
        )
        del request["parameters"][first_mandatory["key"]]
        specs.append({
            "fixture_id": fixture_id,
            "execution_status": "EXECUTED",
            "fixture_kind": "MISSING_MANDATORY",
            "profile": profile,
            "source_sku": anchor["exact_sku"],
            "mutated_parameter": first_mandatory["key"],
            "mutation_method": "REMOVE_REQUIRED_REQUEST_PARAMETER",
            "request": request,
        })

    for index, profile in enumerate(profiles, 1):
        anchor = eligible_by_profile[profile][0]
        contract = contracts[profile]
        fixture_id = f"contradiction:{index:02d}"
        chosen: dict[str, Any] | None = None

        for parameter in contract["parameters"]:
            if parameter["role"] != "MANDATORY":
                continue
            known_numeric = [
                row["facts"][parameter["key"]]["value"]
                for row in by_group[profile]
                if row["facts"].get(parameter["key"], {}).get("state") == "KNOWN"
                and isinstance(row["facts"][parameter["key"]].get("value"), (int, float))
                and not isinstance(row["facts"][parameter["key"]].get("value"), bool)
            ]
            value = numeric_contradiction_value(parameter["comparison_semantics"], known_numeric)
            if value is None:
                continue
            request = build_request(
                fixture_id=fixture_id,
                profile=profile,
                anchor=anchor,
                contract=contract,
                warning="NEGATIVE_MANDATORY_CONTRADICTION_FIXTURE",
            )
            request["parameters"][parameter["key"]] = {
                "state": "PRESENT",
                "value": value,
                "unit": anchor["facts"][parameter["key"]].get("unit"),
                "evidence": [
                    f"integration:r1:numeric-outside-observed-domain:{parameter['key']}"
                ],
            }
            trial = matcher.select(by_group[profile], contract, request)
            if (
                trial.result["result_class"] == "TEST1_NOT_SELECTED"
                and candidate_has_mandatory_contradiction(
                    trial, anchor["exact_sku"], parameter["key"]
                )
            ):
                chosen = {
                    "fixture_id": fixture_id,
                    "execution_status": "EXECUTED",
                    "fixture_kind": "CONTRADICTION",
                    "profile": profile,
                    "source_sku": anchor["exact_sku"],
                    "mutated_parameter": parameter["key"],
                    "mutation_method": "NUMERIC_OUTSIDE_OBSERVED_DOMAIN",
                    "request": request,
                }
                break

        if chosen is None:
            for parameter in contract["parameters"]:
                if parameter["role"] != "MANDATORY" or parameter["comparison_semantics"] != "EQUALS":
                    continue
                anchor_fact = anchor["facts"][parameter["key"]]
                alternate = next(
                    (
                        row
                        for row in by_group[profile]
                        if row["facts"].get(parameter["key"], {}).get("state") == "KNOWN"
                        and row["facts"][parameter["key"]].get("value") != anchor_fact.get("value")
                    ),
                    None,
                )
                if alternate is None:
                    continue
                alt_fact = alternate["facts"][parameter["key"]]
                request = build_request(
                    fixture_id=fixture_id,
                    profile=profile,
                    anchor=anchor,
                    contract=contract,
                    warning="NEGATIVE_MANDATORY_CONTRADICTION_FIXTURE",
                )
                request["parameters"][parameter["key"]] = {
                    "state": "PRESENT",
                    "value": copy.deepcopy(alt_fact.get("value")),
                    "unit": alt_fact.get("unit"),
                    "evidence": list(alt_fact.get("source_refs") or []) + [
                        f"integration:r1:source-backed-alternate:{alternate['exact_sku']}"
                    ],
                }
                trial = matcher.select(by_group[profile], contract, request)
                if (
                    trial.result["result_class"] == "TEST1_NOT_SELECTED"
                    and candidate_has_mandatory_contradiction(
                        trial, anchor["exact_sku"], parameter["key"]
                    )
                ):
                    chosen = {
                        "fixture_id": fixture_id,
                        "execution_status": "EXECUTED",
                        "fixture_kind": "CONTRADICTION",
                        "profile": profile,
                        "source_sku": anchor["exact_sku"],
                        "mutated_parameter": parameter["key"],
                        "mutation_method": "SOURCE_BACKED_ALTERNATE_EQUALS",
                        "request": request,
                    }
                    break

        if chosen is None:
            chosen = {
                "fixture_id": fixture_id,
                "execution_status": "NOT_EXECUTED",
                "fixture_kind": "CONTRADICTION",
                "profile": profile,
                "source_sku": anchor["exact_sku"],
                "mutated_parameter": None,
                "mutation_method": None,
                "reason": NOT_EXECUTED_REASON,
            }
        specs.append(chosen)

    return specs, by_group, contracts


def signature_hash(lines: list[str]) -> str:
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def execute_corpus(
    specs: list[dict[str, Any]],
    *,
    by_group: dict[str, list[dict[str, Any]]],
    contracts: dict[str, dict[str, Any]],
    matcher: Any,
    engine_module: Any,
    full_traces_path: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    full_lines: list[str] = []
    rerun_mismatches = 0
    engine_crashes = 0

    for spec in specs:
        if spec["execution_status"] == "NOT_EXECUTED":
            records.append({
                "fixture_id": spec["fixture_id"],
                "execution_status": "NOT_EXECUTED",
                "fixture_kind": spec["fixture_kind"],
                "profile": spec["profile"],
                "source_sku": spec["source_sku"],
                "mutated_parameter": None,
                "mutation_method": None,
                "reason": spec["reason"],
                "snapshot_ids": {
                    "data_snapshot_id": DATA_SNAPSHOT_ID,
                    "rules_snapshot_id": RULES_SNAPSHOT_ID,
                    "engine_snapshot_id": ENGINE_SNAPSHOT_ID,
                },
            })
            continue

        try:
            run1 = matcher.select(by_group[spec["profile"]], contracts[spec["profile"]], spec["request"])
            run2 = matcher.select(by_group[spec["profile"]], contracts[spec["profile"]], spec["request"])
        except Exception as exc:
            engine_crashes += 1
            records.append({
                "fixture_id": spec["fixture_id"],
                "execution_status": "ENGINE_EXCEPTION",
                "fixture_kind": spec["fixture_kind"],
                "profile": spec["profile"],
                "source_sku": spec["source_sku"],
                "engine_exception": f"{type(exc).__name__}: {exc}",
            })
            continue

        if run1.result != run2.result or run1.trace != run2.trace:
            rerun_mismatches += 1

        record = project_run(spec, run1, engine_module)
        records.append(record)
        if full_traces_path is not None:
            full_lines.append(json.dumps({
                "fixture_id": spec["fixture_id"],
                "fixture_kind": spec["fixture_kind"],
                "profile": spec["profile"],
                "source_sku": spec["source_sku"],
                "request": spec["request"],
                "result": run1.result,
                "trace": run1.trace,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    if full_traces_path is not None:
        full_traces_path.parent.mkdir(parents=True, exist_ok=True)
        full_traces_path.write_text("\n".join(full_lines) + ("\n" if full_lines else ""), encoding="utf-8")

    executed = [r for r in records if r["execution_status"] == "EXECUTED"]
    selected_bad = sum(bool(r["selected_with_mandatory_contradiction"]) for r in executed)

    corpus_lines = [
        "|".join([
            r["fixture_id"],
            r["execution_status"],
            r["fixture_kind"],
            r["profile"],
            r["source_sku"],
            str(r.get("mutated_parameter") or ""),
            str(r.get("mutation_method") or ""),
        ])
        for r in records
    ]
    result_lines = [
        "|".join([
            r["fixture_id"],
            r["result_class"],
            str(r["selected_exact_sku"] or ""),
            "\x1f".join(r["surviving_candidates"]),
            str(r["tie_candidate_count_before_sku_tiebreak"]),
        ])
        for r in executed
    ]
    meta = {
        "fixture_corpus_sha256": signature_hash(corpus_lines),
        "executed_result_sha256": signature_hash(result_lines),
        "deterministic_rerun_mismatches": rerun_mismatches,
        "engine_exceptions_crashes": engine_crashes,
        "selected_with_mandatory_contradiction": selected_bad,
    }
    return records, meta


def build_summary(records: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    profiles = sorted({r["profile"] for r in records}, key=utf8_key)
    executed = [r for r in records if r["execution_status"] == "EXECUTED"]
    per_profile: dict[str, Any] = {}

    for profile in profiles:
        rows = [r for r in records if r["profile"] == profile]
        ex = [r for r in rows if r["execution_status"] == "EXECUTED"]
        pos = [r for r in ex if r["fixture_kind"] == "POSITIVE"]
        miss = [r for r in ex if r["fixture_kind"] == "MISSING_MANDATORY"]
        con = [r for r in ex if r["fixture_kind"] == "CONTRADICTION"]
        not_exec = [r for r in rows if r["fixture_kind"] == "CONTRADICTION" and r["execution_status"] == "NOT_EXECUTED"]
        per_profile[profile] = {
            "eligible_real_sku": len(pos),
            "positive_fixture_count": len(pos),
            "missing_mandatory_fixture_count": len(miss),
            "contradiction_fixture_count": len(con),
            "contradiction_NOT_EXECUTED_count": len(not_exec),
            "TEST1_EXACT": sum(r["result_class"] == "TEST1_EXACT" for r in ex),
            "TEST1_PROBABLE": sum(r["result_class"] == "TEST1_PROBABLE" for r in ex),
            "TEST1_NOT_SELECTED": sum(r["result_class"] == "TEST1_NOT_SELECTED" for r in ex),
            "tie_count": sum(r["tie_candidate_count_before_sku_tiebreak"] > 1 for r in ex),
            "selected_with_mandatory_contradiction": sum(
                bool(r["selected_with_mandatory_contradiction"]) for r in ex
            ),
            "contradiction_NOT_EXECUTED_reason": not_exec[0]["reason"] if not_exec else None,
        }

    summary = {
        "schema_version": "TEST1_NONWEB_INTEGRATION_R1",
        "status": "READY_FOR_ML_TEST_REVIEW",
        "scope": "FROZEN_NON_WEB_INTEGRATION_ONLY",
        "authorities": {
            "issue": 10,
            "start_authority_comment": 5995394958,
            "benchmark_boundary_comment": 5995371435,
            "base_ref": BASE_REF,
            "data_head": DATA_REF,
            "rules_head": RULES_REF,
            "engine_head": ENGINE_REF,
        },
        "snapshot_ids": {
            "data_snapshot_id": DATA_SNAPSHOT_ID,
            "rules_snapshot_id": RULES_SNAPSHOT_ID,
            "engine_snapshot_id": ENGINE_SNAPSHOT_ID,
        },
        "frozen_inputs": [
            {"component": "DATA", "ref": DATA_REF, "path": DATA_CATALOG_PATH, "git_blob_sha1": EXPECTED_BLOBS[(DATA_REF, DATA_CATALOG_PATH)]},
            {"component": "DATA_SUFFICIENCY", "ref": DATA_REF, "path": DATA_SUFFICIENCY_PATH, "git_blob_sha1": EXPECTED_BLOBS[(DATA_REF, DATA_SUFFICIENCY_PATH)]},
            {"component": "RULES", "ref": RULES_REF, "path": RULES_PATH, "git_blob_sha1": EXPECTED_BLOBS[(RULES_REF, RULES_PATH)]},
            {"component": "ENGINE", "ref": ENGINE_REF, "path": ENGINE_PATH, "git_blob_sha1": EXPECTED_BLOBS[(ENGINE_REF, ENGINE_PATH)]},
            {"component": "SHARED_SCHEMA", "ref": BASE_REF, "path": SCHEMA_PATH, "git_blob_sha1": EXPECTED_BLOBS[(BASE_REF, SCHEMA_PATH)]},
        ],
        "cohort": {
            "eligible_real_sku": 367,
            "profiles_exercised": 11,
            "source_backed_mandatory_fact_cells": 1555,
        },
        "fixtures": {
            "positive_fixture_count": sum(r["fixture_kind"] == "POSITIVE" and r["execution_status"] == "EXECUTED" for r in records),
            "missing_mandatory_fixture_count": sum(r["fixture_kind"] == "MISSING_MANDATORY" and r["execution_status"] == "EXECUTED" for r in records),
            "contradiction_fixture_count": sum(r["fixture_kind"] == "CONTRADICTION" and r["execution_status"] == "EXECUTED" for r in records),
            "contradiction_NOT_EXECUTED_count": sum(r["fixture_kind"] == "CONTRADICTION" and r["execution_status"] == "NOT_EXECUTED" for r in records),
            "executed_fixture_count": len(executed),
            "result_record_count_including_NOT_EXECUTED": len(records),
        },
        "results": {
            "TEST1_EXACT": sum(r["result_class"] == "TEST1_EXACT" for r in executed),
            "TEST1_PROBABLE": sum(r["result_class"] == "TEST1_PROBABLE" for r in executed),
            "TEST1_NOT_SELECTED": sum(r["result_class"] == "TEST1_NOT_SELECTED" for r in executed),
            "tie_count": sum(r["tie_candidate_count_before_sku_tiebreak"] > 1 for r in executed),
            "deterministic_rerun_mismatches": meta["deterministic_rerun_mismatches"],
            "selected_with_mandatory_contradiction": meta["selected_with_mandatory_contradiction"],
            "engine_exceptions_crashes": meta["engine_exceptions_crashes"],
        },
        "hashes": {
            "fixture_corpus_sha256": meta["fixture_corpus_sha256"],
            "executed_result_sha256": meta["executed_result_sha256"],
        },
        "per_profile": per_profile,
        "critical_gates": {
            "deterministic_rerun_mismatches_eq_0": meta["deterministic_rerun_mismatches"] == 0,
            "selected_with_mandatory_contradiction_eq_0": meta["selected_with_mandatory_contradiction"] == 0,
            "engine_exceptions_crashes_eq_0": meta["engine_exceptions_crashes"] == 0,
        },
        "trace_persistence": {
            "committed": "integration/r1/TEST1_NONWEB_INTEGRATION_RESULTS_R1.jsonl",
            "committed_projection": "full request, exact result, survivor ordering, and logical GenericMatcher decision_trace_ref",
            "committed_full_traces_gzip": "integration/r1/TEST1_NONWEB_INTEGRATION_FULL_TRACES_R1.jsonl.gz",
            "committed_full_traces_sha256_sidecar": "integration/r1/TEST1_NONWEB_INTEGRATION_FULL_TRACES_R1.jsonl.gz.sha256",
            "full_trace_scope": "verbatim GenericMatcher result + TEST1_DECISION_TRACE_R1 for every executed fixture",
            "materialization": "dedicated exact-head CI generates gzip -n output and commits it on the integration branch; subsequent exact-head CI verifies byte identity",
        },
        "interpretation_limits": [
            "Positive fixtures are mechanically derived from source-backed candidate mandatory facts and prove only frozen seam wiring/execution.",
            "They do not prove expert correctness of real-world selection.",
            "No minimum TEST1_EXACT rate is asserted.",
            "No DATA, RULES, ENGINE, shared schema, semantic classification, or product fact is modified by this task.",
        ],
        "bounded_findings": [
            "installation.tool.axial_system.r1 positive fixtures fail closed because frozen candidate compatibility.size.tuple values are scalar strings while frozen SET_CONTAINS accepts sequence candidates only; this task records but does not repair that frozen seam mismatch."
        ],
    }
    return summary


def validate_expected(summary: dict[str, Any]) -> None:
    if summary["cohort"]["eligible_real_sku"] != 367 or summary["cohort"]["profiles_exercised"] != 11:
        raise AssertionError("cohort gate")
    if summary["fixtures"] != {
        "positive_fixture_count": 367,
        "missing_mandatory_fixture_count": 11,
        "contradiction_fixture_count": 10,
        "contradiction_NOT_EXECUTED_count": 1,
        "executed_fixture_count": 388,
        "result_record_count_including_NOT_EXECUTED": 389,
    }:
        raise AssertionError(f"fixture metrics mismatch: {summary['fixtures']}")
    if summary["results"]["TEST1_EXACT"] != 360:
        raise AssertionError(f"unexpected TEST1_EXACT: {summary['results']}")
    if summary["results"]["TEST1_PROBABLE"] != 0:
        raise AssertionError(f"unexpected TEST1_PROBABLE: {summary['results']}")
    if summary["results"]["TEST1_NOT_SELECTED"] != 28:
        raise AssertionError(f"unexpected TEST1_NOT_SELECTED: {summary['results']}")
    if summary["results"]["tie_count"] != 126:
        raise AssertionError(f"unexpected tie count: {summary['results']}")
    if not all(summary["critical_gates"].values()):
        raise AssertionError(f"critical gate failure: {summary['critical_gates']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    ap.add_argument("--full-traces", type=Path)
    args = ap.parse_args()

    inputs = load_frozen_inputs()
    matcher = inputs["engine_module"].GenericMatcher(inputs["schema"])
    specs, by_group, contracts = build_fixture_specs(inputs, matcher)
    records, meta = execute_corpus(
        specs,
        by_group=by_group,
        contracts=contracts,
        matcher=matcher,
        engine_module=inputs["engine_module"],
        full_traces_path=args.full_traces,
    )
    summary = build_summary(records, meta)
    validate_expected(summary)

    if args.write:
        SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY_PATH.write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        RESULTS_PATH.write_text(
            "".join(
                json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for r in records
            ),
            encoding="utf-8",
        )
    else:
        actual_summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
        actual_records = [
            json.loads(line)
            for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines()
            if line
        ]
        if actual_summary != summary:
            raise AssertionError("committed summary differs from frozen GenericMatcher recomputation")
        if actual_records != records:
            raise AssertionError("committed results JSONL differs from frozen GenericMatcher recomputation")

    print(json.dumps({
        "result": "PASS",
        "eligible_real_sku": summary["cohort"]["eligible_real_sku"],
        "profiles_exercised": summary["cohort"]["profiles_exercised"],
        "positive_fixture_count": summary["fixtures"]["positive_fixture_count"],
        "missing_mandatory_fixture_count": summary["fixtures"]["missing_mandatory_fixture_count"],
        "contradiction_fixture_count": summary["fixtures"]["contradiction_fixture_count"],
        "contradiction_NOT_EXECUTED_count": summary["fixtures"]["contradiction_NOT_EXECUTED_count"],
        **summary["results"],
        **summary["hashes"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
