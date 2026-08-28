"""evalx.independent_oracle: a SECOND, INDEPENDENT implementation of the
connection feasibility verdict.

Why this file exists
--------------------
Every feasibility number RELAY reports so far comes from one engine
(twin/feasibility.py, mirrored by stubs/twin_stub.py). The sweep in
evalx/sweep_local.py labels a scenario "at risk" by calling that same engine,
and then measures whether the agent, which also calls that engine, caught it.
A catch rate of 1.00 obtained that way is true by construction and carries no
information about correctness. This module removes that circularity by
re-deriving the verdict a second time, from the contract prose rather than
from the engine.

Independence discipline (auditable, enforced by evalx/tests/test_independent_oracle.py)
---------------------------------------------------------------------------------------
1. Written from docs/CONTRACT.md section b.1 tool 2 only. The author of this
   module did not read twin/feasibility.py or the feasibility body of
   stubs/twin_stub.py before the first measured run.
2. Module level imports are restricted to `json`, `math` and `datetime`. No
   RELAY code is importable from here, so no shared helper can carry a shared
   bug across the two implementations. In particular the timestamp arithmetic
   is re-derived here rather than reusing stubs.minutes_between.
3. Pure functions over the raw scenario JSON. The only input is the connection
   object as it appears in a world.json document. There is no world state
   overlay, no mutation and no I/O other than the optional CLI.

The rule being re-implemented (docs/CONTRACT.md section b.1, verbatim intent)
----------------------------------------------------------------------------
    completeness_score = sum of weight(f) over evidenced fields f, with weights
    eta 0.30, cut_off 0.25, discharge_estimate 0.15, yard_location 0.15,
    yard_transfer_estimate 0.15 (sum 1.0). If completeness_score < 0.60 the
    verdict is ESCALATE_INSUFFICIENT_EVIDENCE and margin is null. Otherwise
    ready_time = eta + discharge + yard_transfer + restow + buffer_p90 and
    margin_minutes = cut_off - ready_time; margin <= 0 gives INFEASIBLE,
    0 < margin <= 60 gives AT_RISK, otherwise FEASIBLE.

Two readings of "evidenced field" are implemented, because the contract
sentence does not settle the question and an independent implementer has to
choose:

  * FLAG reading (default): a field is evidenced when its boolean in
    connection["evidence"] is true. This is the literal reading of the
    fixture schema.
  * STRICT reading (sensitivity): a field is evidenced when its boolean is
    true AND the value it asserts is actually present in the JSON.

Both are reported. Where the two readings diverge the case is listed, so the
divergence is a measured quantity rather than a hidden assumption.

Fields that carry no completeness weight (restow_minutes, buffer_p90_minutes)
cannot gate the verdict under the contract, so a null value for either is read
as zero minutes and the case is annotated `assumed_zero:<field>`.

CLI (standalone, no RELAY code required)
----------------------------------------
    python evalx/independent_oracle.py --inputs evalx/results/independent-oracle-inputs-n300.json

reads a case dump and prints one verdict object per case, so a reader can
reproduce the grading from the raw inputs and this file alone.
"""

import datetime
import json
import math

ORACLE_VERSION = "1.0.0"
CONTRACT_SOURCE = "docs/CONTRACT.md section b.1 tool 2 (twin.feasibility_check)"

# The five weighted evidence fields and their weights (CONTRACT section b.1 / section h).
COMPLETENESS_WEIGHTS = {
    "eta": 0.30,
    "cut_off": 0.25,
    "discharge_estimate": 0.15,
    "yard_location": 0.15,
    "yard_transfer_estimate": 0.15,
}
ESCALATE_BELOW = 0.60
AT_RISK_MAX_MARGIN_MINUTES = 60.0

VERDICTS = ("FEASIBLE", "AT_RISK", "INFEASIBLE", "ESCALATE_INSUFFICIENT_EVIDENCE")

# Which weighted evidence field asserts which concrete value in the connection JSON.
# yard_location asserts a yard block; it does not enter the ready_time sum.
_VALUE_PATHS = {
    "eta": ("inbound", "eta"),
    "cut_off": ("cut_off",),
    "discharge_estimate": ("estimates", "discharge_minutes"),
    "yard_transfer_estimate": ("estimates", "yard_transfer_minutes"),
    "yard_location": ("yard_block",),
}
# The two ready_time addends the completeness gate protects, plus the two
# unweighted addends that default to zero when absent.
_GATED_ADDENDS = ("discharge_estimate", "yard_transfer_estimate")
_UNGATED_ADDENDS = {"restow_minutes": ("estimates", "restow_minutes"),
                    "buffer_p90_minutes": ("estimates", "buffer_p90_minutes")}


class OracleInputError(ValueError):
    """The raw JSON cannot be read as a connection object at all."""


# ---------------------------------------------------------------------------
# timestamp arithmetic, re-derived (no shared helper with the engine)
# ---------------------------------------------------------------------------
def parse_timestamp(value):
    """Parse an ISO 8601 timestamp with an explicit UTC offset into an aware
    datetime. Naive timestamps are rejected: a connection margin computed
    across an unknown offset would be silently wrong."""
    if not isinstance(value, str):
        raise OracleInputError(f"timestamp must be a string, got {type(value).__name__}")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise OracleInputError(f"unparseable ISO 8601 timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OracleInputError(f"timestamp {value!r} carries no UTC offset")
    return parsed


def minutes_from_to(start_iso, end_iso):
    """Signed minutes from start to end. Positive when end is later."""
    delta = parse_timestamp(end_iso) - parse_timestamp(start_iso)
    return delta.total_seconds() / 60.0


def shift_minutes(start_iso, minutes):
    """The timestamp `minutes` after start, as an aware datetime."""
    return parse_timestamp(start_iso) + datetime.timedelta(minutes=float(minutes))


# ---------------------------------------------------------------------------
# raw JSON accessors
# ---------------------------------------------------------------------------
def _dig(obj, path):
    """Walk a tuple path through nested dicts. Returns None on any miss."""
    node = obj
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _evidence_flags(connection):
    raw = connection.get("evidence")
    if not isinstance(raw, dict):
        raise OracleInputError("connection has no evidence object")
    return {field: bool(raw.get(field, False)) for field in COMPLETENESS_WEIGHTS}


def _value_present(connection, field):
    return _dig(connection, _VALUE_PATHS[field]) is not None


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------
def completeness(connection, strict=False):
    """Return (score, missing_fields, evidenced_map) under the chosen reading."""
    flags = _evidence_flags(connection)
    evidenced = {}
    for field in COMPLETENESS_WEIGHTS:
        ok = flags[field]
        if ok and strict:
            ok = _value_present(connection, field)
        evidenced[field] = ok
    score = 0.0
    for field, weight in COMPLETENESS_WEIGHTS.items():
        if evidenced[field]:
            score += weight
    # The five weights are exact multiples of 0.05, so rounding to 4 places is
    # a display convenience and never changes which side of 0.60 a score lands.
    score = round(score, 4)
    missing = sorted(f for f in COMPLETENESS_WEIGHTS if not evidenced[f])
    return score, missing, evidenced


def _components(connection, notes):
    """The ready_time components as raw values, or (None, unresolvable_fields)
    when a gated component carries no value."""
    eta = _dig(connection, _VALUE_PATHS["eta"])
    cut_off = _dig(connection, _VALUE_PATHS["cut_off"])
    unresolvable = []
    if eta is None:
        unresolvable.append("eta")
    if cut_off is None:
        unresolvable.append("cut_off")
    numbers = {}
    for field in _GATED_ADDENDS:
        value = _dig(connection, _VALUE_PATHS[field])
        if value is None:
            unresolvable.append(field)
        else:
            numbers[field] = float(value)
    for name, path in _UNGATED_ADDENDS.items():
        value = _dig(connection, path)
        if value is None:
            numbers[name] = 0.0
            notes.append(f"assumed_zero:{name}")
        else:
            numbers[name] = float(value)
    if unresolvable:
        return None, sorted(set(unresolvable))
    return {
        "eta": eta,
        "cut_off": cut_off,
        "discharge_minutes": numbers["discharge_estimate"],
        "yard_transfer_minutes": numbers["yard_transfer_estimate"],
        "restow_minutes": numbers["restow_minutes"],
        "buffer_p90_minutes": numbers["buffer_p90_minutes"],
    }, []


def _escalation(connection_id, score, missing, notes, reason):
    return {
        "connection_id": connection_id,
        "verdict": "ESCALATE_INSUFFICIENT_EVIDENCE",
        "feasible": None,
        "margin_minutes": None,
        "ready_time": None,
        "completeness_score": score,
        "components": None,
        "missing_fields": missing,
        "escalation_reason": reason,
        "oracle_notes": sorted(set(notes)),
        "oracle_version": ORACLE_VERSION,
    }


def feasibility(connection, strict=False):
    """The independent FeasibilityResult for one raw connection object.

    Mirrors the FeasibilityResult shape of CONTRACT section b.1 and adds
    `escalation_reason`, `ready_time` and `oracle_notes`, which are oracle
    diagnostics rather than contract fields."""
    if not isinstance(connection, dict):
        raise OracleInputError("connection must be a JSON object")
    connection_id = connection.get("connection_id")
    if not isinstance(connection_id, str):
        raise OracleInputError("connection has no connection_id string")

    notes = []
    score, missing, _ = completeness(connection, strict=strict)
    if score < ESCALATE_BELOW:
        return _escalation(connection_id, score, missing, notes, "completeness_below_gate")

    components, unresolvable = _components(connection, notes)
    if components is None:
        # The evidence flags claim a field that carries no value. The contract
        # forbids guessing a margin, so this escalates too, under its own
        # reason code so the two escalation causes stay separable.
        notes.append("evidence_flag_without_value:" + ",".join(unresolvable))
        return _escalation(connection_id, score, missing, notes, "evidence_flag_without_value")

    added = (components["discharge_minutes"] + components["yard_transfer_minutes"]
             + components["restow_minutes"] + components["buffer_p90_minutes"])
    ready_time = shift_minutes(components["eta"], added)
    margin = (parse_timestamp(components["cut_off"]) - ready_time).total_seconds() / 60.0
    margin = round(margin, 4)

    if margin <= 0.0:
        verdict = "INFEASIBLE"
    elif margin <= AT_RISK_MAX_MARGIN_MINUTES:
        verdict = "AT_RISK"
    else:
        verdict = "FEASIBLE"

    return {
        "connection_id": connection_id,
        "verdict": verdict,
        "feasible": verdict == "FEASIBLE",
        "margin_minutes": margin,
        "ready_time": ready_time.isoformat(),
        "completeness_score": score,
        "components": components,
        "missing_fields": missing,
        "escalation_reason": None,
        "oracle_notes": sorted(set(notes)),
        "oracle_version": ORACLE_VERSION,
    }


def feasibility_from_world(world, connection_id, strict=False):
    """Convenience: find the connection in a raw world.json document."""
    for connection in world.get("connections", []):
        if isinstance(connection, dict) and connection.get("connection_id") == connection_id:
            return feasibility(connection, strict=strict)
    raise OracleInputError(f"connection {connection_id} not present in world")


def at_risk(result):
    """The label the sweep needs: a connection needs attention when it is not
    plainly FEASIBLE. ESCALATE counts, because refusing to compute a margin on
    thin evidence is exactly the case a guardian must not drop."""
    return result["verdict"] in ("AT_RISK", "INFEASIBLE", "ESCALATE_INSUFFICIENT_EVIDENCE")


# ---------------------------------------------------------------------------
# comparison against another implementation's result
# ---------------------------------------------------------------------------
MARGIN_TOLERANCE_MINUTES = 0.1


def compare(independent, engine):
    """Classify one (independent, engine) pair. `engine` is any dict carrying
    at least `verdict`; `margin_minutes` and `completeness_score` are compared
    when both sides have them."""
    verdict_match = independent["verdict"] == engine.get("verdict")
    ind_margin = independent["margin_minutes"]
    eng_margin = engine.get("margin_minutes")
    if ind_margin is None and eng_margin is None:
        margin_match = True
        margin_delta = None
    elif ind_margin is None or eng_margin is None:
        margin_match = False
        margin_delta = None
    else:
        margin_delta = round(float(eng_margin) - float(ind_margin), 4)
        margin_match = math.isclose(float(eng_margin), float(ind_margin),
                                    rel_tol=0.0, abs_tol=MARGIN_TOLERANCE_MINUTES)
    ind_score = independent["completeness_score"]
    eng_score = engine.get("completeness_score")
    if eng_score is None:
        completeness_match = None
        completeness_delta = None
    else:
        completeness_delta = round(float(eng_score) - float(ind_score), 6)
        completeness_match = math.isclose(float(eng_score), float(ind_score),
                                          rel_tol=0.0, abs_tol=1e-6)
    if verdict_match and margin_match and completeness_match is not False:
        classification = "agree"
    elif not verdict_match:
        classification = f"verdict:{independent['verdict']}->{engine.get('verdict')}"
    elif not margin_match:
        classification = "margin_only"
    else:
        classification = "completeness_only"
    return {
        "verdict_match": verdict_match,
        "margin_match": margin_match,
        "completeness_match": completeness_match,
        "margin_delta_minutes": margin_delta,
        "completeness_delta": completeness_delta,
        "classification": classification,
        "agree": classification == "agree",
    }


# ---------------------------------------------------------------------------
# standalone CLI over a case dump
# ---------------------------------------------------------------------------
def grade_cases(cases, strict=False):
    """Grade a list of {scenario_id, connection} dump entries."""
    out = []
    for case in cases:
        result = feasibility(case["connection"], strict=strict)
        out.append({"scenario_id": case.get("scenario_id"),
                    "connection_id": result["connection_id"],
                    "independent": result})
    return out


def _main(argv=None):
    import sys  # local: keeps the module level import list to json, math, datetime
    args = list(sys.argv[1:] if argv is None else argv)
    inputs = None
    strict = False
    summary_only = False
    while args:
        token = args.pop(0)
        if token == "--inputs":
            inputs = args.pop(0)
        elif token == "--strict":
            strict = True
        elif token == "--summary":
            summary_only = True
        else:
            print(f"unknown argument {token}", file=sys.stderr)
            return 2
    if inputs is None:
        print("usage: independent_oracle.py --inputs <dump.json> [--strict] [--summary]",
              file=sys.stderr)
        return 2
    with open(inputs, "r", encoding="utf-8") as handle:
        dump = json.load(handle)
    graded = grade_cases(dump["cases"], strict=strict)
    if summary_only:
        counts = {}
        for row in graded:
            verdict = row["independent"]["verdict"]
            counts[verdict] = counts.get(verdict, 0) + 1
        print(json.dumps({"oracle_version": ORACLE_VERSION, "strict": strict,
                          "n_cases": len(graded), "verdict_mix": counts}, indent=2))
    else:
        print(json.dumps({"oracle_version": ORACLE_VERSION, "strict": strict,
                          "contract_source": CONTRACT_SOURCE, "graded": graded}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
