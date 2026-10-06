from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

ENGINE_SNAPSHOT_ID = "TEST1_ENGINE_GENERIC_MATCHER_R1_v2"


class MatcherInputError(ValueError):
    """Raised when schema-valid records are mutually inconsistent for one run."""


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _equals(candidate: Any, request: Any) -> bool:
    return candidate == request


def _numeric_equal(candidate: Any, request: Any) -> bool:
    return _is_number(candidate) and _is_number(request) and float(candidate) == float(request)


def _candidate_gte_request(candidate: Any, request: Any) -> bool:
    return _is_number(candidate) and _is_number(request) and float(candidate) >= float(request)


def _candidate_lte_request(candidate: Any, request: Any) -> bool:
    return _is_number(candidate) and _is_number(request) and float(candidate) <= float(request)


def _range_contains_request(candidate: Any, request: Any) -> bool:
    if not isinstance(candidate, Mapping) or not _is_number(request):
        return False
    low = candidate.get("min")
    high = candidate.get("max")
    if not _is_number(low) or not _is_number(high):
        return False
    return float(low) <= float(request) <= float(high)


def _set_contains(candidate: Any, request: Any) -> bool:
    if isinstance(candidate, (str, bytes)) or not isinstance(candidate, Sequence):
        return False
    return request in candidate


SEMANTIC_DISPATCH: Mapping[str, Callable[[Any, Any], bool]] = {
    "EQUALS": _equals,
    "NUMERIC_EQUAL": _numeric_equal,
    "CANDIDATE_GTE_REQUEST": _candidate_gte_request,
    "CANDIDATE_LTE_REQUEST": _candidate_lte_request,
    "RANGE_CONTAINS_REQUEST": _range_contains_request,
    "SET_CONTAINS": _set_contains,
}

SUPPORTED_COMPARISON_SEMANTICS = frozenset(SEMANTIC_DISPATCH)


@dataclass(frozen=True)
class MatchRun:
    result: dict[str, Any]
    trace: dict[str, Any]


class GenericMatcher:
    """Schema-validating deterministic TEST-1 matcher.

    The matcher contains no product-group branches. Product-group behavior is declared
    only by TEST1_GROUP_CONTRACT comparison semantics and score policy.
    """

    def __init__(self, shared_schema: Mapping[str, Any], engine_snapshot_id: str = ENGINE_SNAPSHOT_ID):
        self._schema = dict(shared_schema)
        self.engine_snapshot_id = engine_snapshot_id
        if "$defs" not in self._schema:
            raise MatcherInputError("shared schema has no $defs")

    def _validate(self, definition: str, instance: Mapping[str, Any]) -> None:
        if definition not in self._schema["$defs"]:
            raise MatcherInputError(f"schema definition not found: {definition}")
        wrapper = {
            "$schema": self._schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
            "$defs": self._schema["$defs"],
            "$ref": f"#/$defs/{definition}",
        }
        Draft202012Validator(wrapper).validate(instance)

    @staticmethod
    def _data_snapshot_id(products: Sequence[Mapping[str, Any]]) -> str:
        snapshots = {str(product["data_snapshot_id"]) for product in products}
        if not snapshots:
            return "TEST1_EMPTY_DATA_SNAPSHOT"
        if len(snapshots) != 1:
            raise MatcherInputError(f"mixed data_snapshot_id values: {sorted(snapshots)}")
        return next(iter(snapshots))

    @staticmethod
    def _ensure_unique_skus(products: Sequence[Mapping[str, Any]]) -> None:
        seen: set[str] = set()
        dupes: set[str] = set()
        for product in products:
            sku = str(product["exact_sku"])
            if sku in seen:
                dupes.add(sku)
            seen.add(sku)
        if dupes:
            raise MatcherInputError(f"duplicate exact_sku values: {sorted(dupes, key=_utf8_key)}")

    @staticmethod
    def _unit_compatible(parameter: Mapping[str, Any], candidate_fact: Mapping[str, Any], request_fact: Mapping[str, Any]) -> bool:
        canonical = parameter.get("canonical_unit")
        if canonical is None:
            return candidate_fact.get("unit") == request_fact.get("unit") or (
                candidate_fact.get("unit") is None and request_fact.get("unit") is None
            )
        return candidate_fact.get("unit") == canonical and request_fact.get("unit") == canonical

    def _compare_known(
        self,
        parameter: Mapping[str, Any],
        candidate_fact: Mapping[str, Any],
        request_fact: Mapping[str, Any],
    ) -> tuple[str, str]:
        semantics = str(parameter["comparison_semantics"])
        comparator = SEMANTIC_DISPATCH.get(semantics)
        if comparator is None:
            raise MatcherInputError(f"unsupported comparison_semantics: {semantics}")
        if not self._unit_compatible(parameter, candidate_fact, request_fact):
            return "CONTRADICTION", "unit_mismatch_or_noncanonical_unit"
        matched = comparator(candidate_fact.get("value"), request_fact.get("value"))
        return ("MATCH", f"semantic_match:{semantics}") if matched else (
            "CONTRADICTION",
            f"semantic_contradiction:{semantics}",
        )

    @staticmethod
    def contract_capability(contract: Mapping[str, Any]) -> dict[str, Any]:
        unsupported = sorted(
            {
                str(parameter["comparison_semantics"])
                for parameter in contract["parameters"]
                if str(parameter["comparison_semantics"]) not in SUPPORTED_COMPARISON_SEMANTICS
            },
            key=_utf8_key,
        )
        return {
            "supported": not unsupported,
            "supported_semantics": sorted(SUPPORTED_COMPARISON_SEMANTICS, key=_utf8_key),
            "unsupported_semantics": unsupported,
        }

    def _request_not_constraining(
        self,
        parameter: Mapping[str, Any],
        request_fact: Mapping[str, Any] | None,
    ) -> tuple[bool, str, str]:
        state = "ABSENT" if request_fact is None else str(request_fact["state"])
        if state == "PRESENT":
            return False, state, ""
        behavior = parameter.get("request_missing_behavior", "NO_CONSTRAINT")
        if behavior == "NOT_SELECTED":
            outcome = "MISSING" if state == "ABSENT" else "AMBIGUOUS"
            return True, state, f"REQUEST_{outcome}_REQUIRES_NOT_SELECTED"
        if behavior == "WARN":
            return False, state, f"REQUEST_{state}_NOT_CONSTRAINING_WARNING"
        return False, state, f"REQUEST_{state}_NOT_CONSTRAINING"

    def select(
        self,
        products: Sequence[Mapping[str, Any]],
        contract: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> MatchRun:
        for product in products:
            self._validate("TEST1_PRODUCT_RECORD", product)
        self._validate("TEST1_GROUP_CONTRACT", contract)
        self._validate("TEST1_REQUEST_FACTS", request)
        self._ensure_unique_skus(products)

        data_snapshot_id = self._data_snapshot_id(products)
        request_id = str(request["request_id"])
        position_id = str(request["position_id"])
        detected = request["detected_group"]
        group_state = str(detected["state"])
        detected_group = detected.get("group_id")
        trace_ref = f"trace:{request_id}:{position_id}:{self.engine_snapshot_id}"

        if group_state != "KNOWN" or detected_group is None:
            return self._finalize(
                request=request,
                contract=contract,
                data_snapshot_id=data_snapshot_id,
                detected_group=detected_group,
                candidate_decisions=[],
                survivors=[],
                score_breakdown={"candidate_totals": {}, "selection_order": []},
                selected=None,
                warnings=[f"GROUP_{group_state}_FAIL_CLOSED"],
                trace_ref=trace_ref,
            )

        if detected_group != contract["group_id"]:
            return self._finalize(
                request=request,
                contract=contract,
                data_snapshot_id=data_snapshot_id,
                detected_group=str(detected_group),
                candidate_decisions=[],
                survivors=[],
                score_breakdown={"candidate_totals": {}, "selection_order": []},
                selected=None,
                warnings=["DETECTED_GROUP_HAS_NO_MATCHING_CONTRACT"],
                trace_ref=trace_ref,
            )

        group_products = sorted(
            (product for product in products if product["group_id"] == detected_group),
            key=lambda product: _utf8_key(str(product["exact_sku"])),
        )

        capability = self.contract_capability(contract)
        if not capability["supported"]:
            return self._unsupported_contract_run(
                request=request,
                contract=contract,
                data_snapshot_id=data_snapshot_id,
                detected_group=str(detected_group),
                group_products=group_products,
                capability=capability,
                trace_ref=trace_ref,
            )

        candidate_decisions: list[dict[str, Any]] = []
        result_warning_candidates: dict[str, list[str]] = {}
        score_policy = contract["score_policy"]

        for product in group_products:
            sku = str(product["exact_sku"])
            excluded = False
            exact_eligible = True
            probable_eligible = True
            total_score = 0.0
            comparisons: list[dict[str, Any]] = []
            reasons: list[str] = []
            candidate_warnings: list[str] = []

            for parameter in contract["parameters"]:
                key = str(parameter["key"])
                role = str(parameter["role"])
                request_fact = request["parameters"].get(key)
                force_not_selected, request_state, request_reason = self._request_not_constraining(parameter, request_fact)

                if request_state != "PRESENT":
                    outcome = "MISSING" if request_state == "ABSENT" else "AMBIGUOUS"
                    if not force_not_selected:
                        outcome = "NOT_CONSTRAINED"
                    comparisons.append(
                        {
                            "key": key,
                            "role": role,
                            "request_state": request_state,
                            "candidate_state": str(product["facts"].get(key, {"state": "MISSING"})["state"]),
                            "outcome": outcome,
                            "score_delta": 0.0,
                            "reason": request_reason,
                        }
                    )
                    reasons.append(request_reason)
                    if request_reason.endswith("WARNING"):
                        candidate_warnings.append(request_reason)
                    if force_not_selected:
                        excluded = True
                        exact_eligible = False
                        probable_eligible = False
                    continue

                candidate_fact = product["facts"].get(
                    key,
                    {"state": "MISSING", "source_refs": [], "unit": None, "warning": "fact_key_absent"},
                )
                candidate_state = str(candidate_fact["state"])
                score_delta = 0.0

                if candidate_state != "KNOWN":
                    outcome = "MISSING" if candidate_state == "MISSING" else "AMBIGUOUS"
                    reason = f"CANDIDATE_{candidate_state}:{key}"
                    if role == "MANDATORY":
                        exact_eligible = False
                        behavior = str(parameter["missing_candidate_fact_behavior"])
                        candidate_warnings.append(f"MANDATORY_{candidate_state}:{key}:{behavior}")
                        if behavior == "EXCLUDE":
                            excluded = True
                            probable_eligible = False
                            reason = f"MANDATORY_{candidate_state}_EXCLUDED:{key}"
                        else:
                            reason = f"MANDATORY_{candidate_state}_PROBABLE_ONLY:{key}"
                    else:
                        score_delta = float(score_policy["additional_missing"])
                        reason = f"ADDITIONAL_{candidate_state}_WARNING:{key}"
                        candidate_warnings.append(reason)
                    total_score += score_delta
                    comparisons.append(
                        {
                            "key": key,
                            "role": role,
                            "request_state": "PRESENT",
                            "candidate_state": candidate_state,
                            "outcome": outcome,
                            "score_delta": score_delta,
                            "reason": reason,
                        }
                    )
                    reasons.append(reason)
                    continue

                outcome, reason = self._compare_known(parameter, candidate_fact, request_fact)
                if role == "MANDATORY":
                    if outcome == "CONTRADICTION":
                        excluded = True
                        exact_eligible = False
                        probable_eligible = False
                        reason = f"MANDATORY_CONTRADICTION:{key}:{reason}"
                else:
                    if outcome == "MATCH":
                        score_delta = float(score_policy["additional_match"])
                    else:
                        score_delta = float(score_policy.get("additional_contradiction", 0.0))
                    total_score += score_delta

                comparisons.append(
                    {
                        "key": key,
                        "role": role,
                        "request_state": "PRESENT",
                        "candidate_state": "KNOWN",
                        "outcome": outcome,
                        "score_delta": score_delta,
                        "reason": reason,
                    }
                )
                reasons.append(reason)

            if excluded:
                exact_eligible = False
                probable_eligible = False

            candidate_decisions.append(
                {
                    "exact_sku": sku,
                    "excluded": excluded,
                    "exact_eligible": exact_eligible,
                    "probable_eligible": probable_eligible,
                    "comparisons": comparisons,
                    "total_score": total_score,
                    "reasons": reasons,
                }
            )
            result_warning_candidates[sku] = candidate_warnings

        decision_by_sku = {decision["exact_sku"]: decision for decision in candidate_decisions}
        survivors_decisions = [
            decision for decision in candidate_decisions if not decision["excluded"] and decision["probable_eligible"]
        ]
        survivors_decisions.sort(
            key=lambda decision: (
                0 if decision["exact_eligible"] else 1,
                -float(decision["total_score"]),
                _utf8_key(str(decision["exact_sku"])),
            )
        )
        survivors = [str(decision["exact_sku"]) for decision in survivors_decisions]
        selected = survivors_decisions[0] if survivors_decisions else None
        warnings = list(request.get("warnings", []))
        if selected is not None:
            warnings.extend(result_warning_candidates[selected["exact_sku"]])

        score_breakdown = {
            "candidate_totals": {
                decision["exact_sku"]: decision["total_score"]
                for decision in candidate_decisions
            },
            "selection_order": survivors,
            "selection_precedence": [
                "exact_eligible_desc",
                "total_score_desc",
                "exact_sku_utf8_byte_order_asc",
            ],
        }

        run = self._finalize(
            request=request,
            contract=contract,
            data_snapshot_id=data_snapshot_id,
            detected_group=str(detected_group),
            candidate_decisions=candidate_decisions,
            survivors=survivors,
            score_breakdown=score_breakdown,
            selected=selected,
            warnings=warnings,
            trace_ref=trace_ref,
        )

        if run.result["selected_exact_sku"] is not None:
            selected_decision = decision_by_sku[run.result["selected_exact_sku"]]
            if any(
                comparison["role"] == "MANDATORY" and comparison["outcome"] == "CONTRADICTION"
                for comparison in selected_decision["comparisons"]
            ):
                raise RuntimeError("safety invariant violated: selected candidate has mandatory contradiction")
        return run

    def _unsupported_contract_run(
        self,
        *,
        request: Mapping[str, Any],
        contract: Mapping[str, Any],
        data_snapshot_id: str,
        detected_group: str,
        group_products: Sequence[Mapping[str, Any]],
        capability: Mapping[str, Any],
        trace_ref: str,
    ) -> MatchRun:
        unsupported = set(str(value) for value in capability["unsupported_semantics"])
        unsupported_parameters = [
            parameter
            for parameter in contract["parameters"]
            if str(parameter["comparison_semantics"]) in unsupported
        ]
        decisions: list[dict[str, Any]] = []
        warnings = [
            f"UNSUPPORTED_COMPARISON_SEMANTICS:{parameter['key']}:{parameter['comparison_semantics']}"
            for parameter in unsupported_parameters
        ]
        for product in group_products:
            comparisons = []
            reasons = []
            for parameter in unsupported_parameters:
                key = str(parameter["key"])
                request_fact = request["parameters"].get(key)
                request_state = "ABSENT" if request_fact is None else str(request_fact["state"])
                candidate_state = str(product["facts"].get(key, {"state": "MISSING"})["state"])
                reason = f"UNSUPPORTED_COMPARISON_SEMANTICS:{key}:{parameter['comparison_semantics']}"
                comparisons.append(
                    {
                        "key": key,
                        "role": str(parameter["role"]),
                        "request_state": request_state,
                        "candidate_state": candidate_state,
                        "outcome": "AMBIGUOUS",
                        "score_delta": 0.0,
                        "reason": reason,
                    }
                )
                reasons.append(reason)
            decisions.append(
                {
                    "exact_sku": str(product["exact_sku"]),
                    "excluded": True,
                    "exact_eligible": False,
                    "probable_eligible": False,
                    "comparisons": comparisons,
                    "total_score": 0.0,
                    "reasons": reasons,
                }
            )
        return self._finalize(
            request=request,
            contract=contract,
            data_snapshot_id=data_snapshot_id,
            detected_group=detected_group,
            candidate_decisions=decisions,
            survivors=[],
            score_breakdown={
                "candidate_totals": {decision["exact_sku"]: 0.0 for decision in decisions},
                "selection_order": [],
                "contract_capability": dict(capability),
                "fail_closed_reason": "UNSUPPORTED_COMPARISON_SEMANTICS",
            },
            selected=None,
            warnings=warnings,
            trace_ref=trace_ref,
        )

    def _finalize(
        self,
        *,
        request: Mapping[str, Any],
        contract: Mapping[str, Any],
        data_snapshot_id: str,
        detected_group: str | None,
        candidate_decisions: list[dict[str, Any]],
        survivors: list[str],
        score_breakdown: dict[str, Any],
        selected: Mapping[str, Any] | None,
        warnings: Iterable[str],
        trace_ref: str,
    ) -> MatchRun:
        warnings_out = list(dict.fromkeys(str(warning) for warning in warnings))
        if selected is None:
            result_class = "TEST1_NOT_SELECTED"
            selected_sku = None
        elif bool(selected["exact_eligible"]):
            result_class = "TEST1_EXACT"
            selected_sku = str(selected["exact_sku"])
        else:
            result_class = "TEST1_PROBABLE"
            selected_sku = str(selected["exact_sku"])

        trace = {
            "schema_version": "TEST1_DECISION_TRACE_R1",
            "request_id": str(request["request_id"]),
            "position_id": str(request["position_id"]),
            "detected_group": detected_group,
            "candidate_count_before_filtering": len(candidate_decisions),
            "candidate_decisions": candidate_decisions,
            "surviving_candidates": survivors,
            "score_breakdown": score_breakdown,
            "data_snapshot_id": data_snapshot_id,
            "rules_snapshot_id": str(contract["rules_snapshot_id"]),
            "engine_snapshot_id": self.engine_snapshot_id,
        }
        result = {
            "schema_version": "TEST1_SELECTION_RESULT_R1",
            "request_id": str(request["request_id"]),
            "position_id": str(request["position_id"]),
            "result_class": result_class,
            "selected_exact_sku": selected_sku,
            "warnings": warnings_out,
            "processing_time_ms": 0.0,
            "data_snapshot_id": data_snapshot_id,
            "rules_snapshot_id": str(contract["rules_snapshot_id"]),
            "engine_snapshot_id": self.engine_snapshot_id,
            "decision_trace_ref": trace_ref,
        }
        self._validate("TEST1_DECISION_TRACE", trace)
        self._validate("TEST1_SELECTION_RESULT", result)
        if result_class == "TEST1_NOT_SELECTED" and selected_sku is not None:
            raise RuntimeError("safety invariant violated: NOT_SELECTED with non-null SKU")
        return MatchRun(result=result, trace=trace)


def count_selected_with_mandatory_contradiction(run: MatchRun) -> int:
    selected = run.result["selected_exact_sku"]
    if selected is None:
        return 0
    for decision in run.trace["candidate_decisions"]:
        if decision["exact_sku"] != selected:
            continue
        return int(
            any(
                comparison["role"] == "MANDATORY" and comparison["outcome"] == "CONTRADICTION"
                for comparison in decision["comparisons"]
            )
        )
    raise RuntimeError("selected SKU absent from candidate decisions")


def build_position_decision_record(
    request: Mapping[str, Any],
    contract: Mapping[str, Any],
    run: MatchRun,
) -> dict[str, Any]:
    """Deterministically join REQUEST_FACTS + contract metadata + ENGINE outputs.

    This is a persistence representation only. It intentionally contains no
    selection business logic and does not replace any shared TEST1_* schema.
    """

    request_id = str(request["request_id"])
    position_id = str(request["position_id"])
    if run.result["request_id"] != request_id or run.trace["request_id"] != request_id:
        raise MatcherInputError("decision record request_id mismatch")
    if run.result["position_id"] != position_id or run.trace["position_id"] != position_id:
        raise MatcherInputError("decision record position_id mismatch")
    snapshot_fields = ("data_snapshot_id", "rules_snapshot_id", "engine_snapshot_id")
    for field in snapshot_fields:
        if run.result[field] != run.trace[field]:
            raise MatcherInputError(f"decision record snapshot mismatch: {field}")
    if str(contract["rules_snapshot_id"]) != run.result["rules_snapshot_id"]:
        raise MatcherInputError("decision record contract rules_snapshot_id mismatch")

    rejection_reasons = {
        decision["exact_sku"]: list(decision["reasons"])
        for decision in run.trace["candidate_decisions"]
        if decision["excluded"]
    }
    return {
        "request_id": request_id,
        "position_id": position_id,
        "raw_request_text": str(request["raw_request_text"]),
        "detected_group": dict(request["detected_group"]),
        "contract_id": str(contract["contract_id"]),
        "contract_group_id": str(contract["group_id"]),
        "contract_etim_mapping": dict(contract["etim_mapping"]),
        "extracted_request_facts": dict(request["parameters"]),
        "candidate_count_before_filtering": run.trace["candidate_count_before_filtering"],
        "candidate_decisions": list(run.trace["candidate_decisions"]),
        "rejection_reasons": rejection_reasons,
        "surviving_candidates": list(run.trace["surviving_candidates"]),
        "score_breakdown": dict(run.trace["score_breakdown"]),
        "selected_exact_sku": run.result["selected_exact_sku"],
        "result_class": run.result["result_class"],
        "warnings": list(run.result["warnings"]),
        "processing_time_ms": run.result["processing_time_ms"],
        "data_snapshot_id": run.result["data_snapshot_id"],
        "rules_snapshot_id": run.result["rules_snapshot_id"],
        "engine_snapshot_id": run.result["engine_snapshot_id"],
        "decision_trace_ref": run.result["decision_trace_ref"],
    }
