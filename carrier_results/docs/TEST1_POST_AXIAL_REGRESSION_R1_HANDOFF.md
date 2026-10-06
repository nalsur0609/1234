# TEST1-REGRESSION-1 / POST-AXIAL CROSS-WORKSTREAM REGRESSION R1 — HANDOFF

Status: READY FOR ML TEST REVIEW

Authority:
- Issue #18
- ML TEST START authority: 6011460238
- benchmark isolation boundary: 5995371435
- exact TEST1 base: `ee7ecee7b7914fd088565f25b61c900d7ce37377`

Frozen workstream authorities:
- DATA `6ffbef9b38f3d79bcb23a5c9b3b42e451dc22e80`
- repaired RULES `0af202cf9069db0ac7952d4d07857540bfffae7a`
- ENGINE `e72f9d8ef0ba6fb61f21b432a189ff692b8ebfd9`
- previous non-WEB integration reference `3132e15e7651b97ebe15a27c99f71034b087ae7d`
- WEB source reference `f4c55387a56ec1932d636c3f4659e0ee4cf9f12b`
- scenario corpus reference `a86e54087f69e6ffd46a13c966ed2b00a1073b31`

Execution binding:
- private TEST1 remains SSOT; the public carrier is compute-only under resume authority 6018146028;
- DATA catalog input is a bounded exact-row projection of the pinned DATA blob covering all current-WEB-executable profiles;
- ENGINE / WEB / scenario / schema / repaired RULES source files are byte-exact copies checked by Git blob SHA-1;
- accepted WEB source is not modified in the repository;
- regression runtime binds only the accepted repaired RULES ref/blob/snapshot;
- the original scenario input/oracle artifacts remain unchanged;
- only the seven Issue #18 axial scenario IDs receive the explicit ML-authority expectation override.

## Regression A — non-WEB
- real DATA-sufficient positives: 367
- profiles: 11
- positive TEST1_EXACT: 367
- positive TEST1_PROBABLE: 0
- positive TEST1_NOT_SELECTED: 0
- missing-mandatory executed / NOT_SELECTED: 11 / 11
- contradiction executed / NOT_SELECTED: 10 / 10
- contradiction NOT_EXECUTED: 1
- deterministic mismatches: 0
- selected_with_mandatory_contradiction: 0
- engine crashes: 0

## Regression B — live WEB
- server started: true
- positive profiles: 11
- TEST1_EXACT: 11
- axial result: TEST1_EXACT
- missing mandatory: TEST1_NOT_SELECTED
- contradiction: TEST1_NOT_SELECTED
- unknown profile: TEST1_NOT_SELECTED
- deterministic mismatches: 0
- selected_with_mandatory_contradiction: 0
- engine crashes: 0
- repaired RULES authority exact: true

The accepted WEB source contains a historical `known_frozen_findings` presentation field describing the pre-repair axial fail-closed state. It is preserved because this regression is forbidden to patch WEB source. Runtime authority ref/blob/snapshot and actual selection execution are separately verified against the repaired RULES authority.

## Regression C — 120 synthetic scenarios
- total: 120
- executed: 93
- NOT_EXECUTED_CURRENT_SCOPE: 27
- TEST1_EXACT: 52
- TEST1_PROBABLE: 0
- TEST1_NOT_SELECTED: 41
- axial override exact: 7 / 7
- deterministic mismatches: 0
- selected_with_mandatory_contradiction: 0
- engine crashes: 0
- unresolved mismatch/defect count: 0

## Consistency
- DATA source-row mutation: none; carrier uses the bounded exact-row profile projection
- ENGINE drift: none
- WEB source drift: none
- scenario input drift: none
- repaired RULES blob/ref/snapshot: exact
- stale old RULES snapshot executed: no

Dedicated workflow:
`.github/workflows/test1-post-axial-regression-r1.yml`

Exact-head CI run and PR/HEAD identity are recorded in the final Issue #18 handoff comment after committed-artifact verification.

No accepted DATA/RULES/ENGINE/WEB/SCENARIOS/QA/shared-schema artifact was modified.
No matcher or selection semantics were changed.
No benchmark integration.
No merge.
No STABLE declaration.

`TEST1_POST_AXIAL_CROSS_WORKSTREAM_REGRESSION_R1 = READY_FOR_ML_TEST_REVIEW`
