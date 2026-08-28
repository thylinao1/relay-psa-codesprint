#!/usr/bin/env python3
"""RELAY advisory generator: messy free-text carrier advisories / port notices.

Produces 60+ SYNTHETIC advisories in the CONTRACT §a7 shape (advisory_id,
received_at, source, free_text) plus eval-side ground truth, across the five
contracted messiness classes:

  vessel_name_variant:    'MERLION EXPRESS' / 'MERLION EXP' / 'MLX 437-W' / 'M.EXPRESS'
  voyage_code_confusion:  '437W' vs '0437W' vs '437-W' vs 'V.437W' vs '437E??'
  partial_rotation:       'rotation may drop PKG next call, TBC'
  contradiction_vs_ais:   advisory ETA disagrees with the AIS-derived estimate
  ambiguous_cutoff:       'cutoff unchanged 26/08 0226 hrs??' / docs-vs-cargo confusion

Determinism: a fixed seed drives every template/variant/time choice. The optional
llama3.2:3b paraphrase step (local Ollama HTTP) is CACHED in data/advisories_cache.json
keyed by sha256(template text), so regeneration is byte-identical; with Ollama down
(or --no-llm / RELAY_NO_OLLAMA=1) the templates alone suffice. A paraphrase is only
accepted if every load-bearing token (names, codes, times) survives verbatim,
otherwise the template text ships and the record says so.

Every record carries data_provenance: "SYNTHETIC" (SPEC CON-5).

Usage:
    python3 data/advisories.py                 # 64 records -> data/advisories.json
    python3 data/advisories.py --no-llm        # template-only, no HTTP
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(_HERE, "advisories.json")
DEFAULT_CACHE = os.path.join(_HERE, "advisories_cache.json")

SEED = 42
DEFAULT_COUNT = 64
SGT = timezone(timedelta(hours=8))
BASE_DAY = datetime(2026, 8, 25, 6, 0, tzinfo=SGT)

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"

MESSINESS_CLASSES = [
    "vessel_name_variant",
    "voyage_code_confusion",
    "partial_rotation",
    "contradiction_vs_ais",
    "ambiguous_cutoff",
]

# Synthetic fleet. The first four mirror stubs/fixtures/world.json so a slice of
# the batch is directly usable against the frozen world; the rest are fictional.
FLEET = [
    {"name": "MERLION EXPRESS", "code": "MLX", "voyage": "437W", "imo": "9700001"},
    {"name": "TEMASEK STAR", "code": "TMS", "voyage": "0402E", "imo": "9700002"},
    {"name": "RAFFLES WAVE", "code": "RFW", "voyage": "0511E", "imo": "9700003"},
    {"name": "SEA PANTHER", "code": "SPR", "voyage": "12E", "imo": None},
    {"name": "LION CITY GLORY", "code": "LCG", "voyage": "221N", "imo": "9700104"},
    {"name": "SENTOSA BREEZE", "code": "STB", "voyage": "078E", "imo": "9700105"},
    {"name": "KALLANG SPIRIT", "code": "KLS", "voyage": "310W", "imo": "9700106"},
    {"name": "JURONG PIONEER", "code": "JRP", "voyage": "445S", "imo": "9700107"},
    {"name": "MARINA CREST", "code": "MRC", "voyage": "052W", "imo": "9700108"},
    {"name": "CHANGI HORIZON", "code": "CGH", "voyage": "9012E", "imo": "9700109"},
    {"name": "PUNGGOL DAWN", "code": "PGD", "voyage": "184E", "imo": "9700110"},
    {"name": "BUKIT TRADER", "code": "BKT", "voyage": "673W", "imo": "9700111"},
]

SOURCES = [
    "carrier_email:oceanlink-sg-ops-desk",
    "carrier_email:eastwind-lines-cs",
    "agency_email:sg-agency-desk-ops",
    "port_notice:harbour-ops-circular",
    "carrier_email:meridian-shipping-sg",
    "agency_email:straits-forwarding-desk",
]

ROTATION_PORTS = ["PKG", "TPP", "HKG", "LCB", "CMB", "JKT", "VUT"]


# ---------------------------------------------------------------------------
# deterministic messiness helpers
# ---------------------------------------------------------------------------
def name_variant(rng: random.Random, vessel: dict) -> str:
    name = vessel["name"]
    words = name.split()
    choices = [
        name,
        f"MV {name}",
        f"{words[0]} {words[1][:3]}." if len(words) > 1 and len(words[1]) > 3 else name,
        f"{words[0][0]}.{' '.join(words[1:])}" if len(words) > 1 else name,
        f"{vessel['code']} {vessel['voyage']}",
        name.title(),
        f"'{words[0]}'",
    ]
    return rng.choice(choices)


def voyage_variant(rng: random.Random, voyage: str) -> str:
    flipped = voyage[:-1] + ("E" if voyage.endswith("W") else "W")
    choices = [
        voyage,
        f"0{voyage}" if not voyage.startswith("0") else voyage.lstrip("0"),
        f"{voyage[:-1]}-{voyage[-1]}",
        f"V.{voyage}",
        f"v{voyage.lower()}",
        f"{flipped}??",
    ]
    return rng.choice(choices)


def fmt_dm(dt: datetime) -> str:
    return dt.strftime("%d/%m")


def fmt_hm(dt: datetime) -> str:
    return dt.strftime("%H%M")


# ---------------------------------------------------------------------------
# templates: each returns (free_text, ground_truth, classes, must_keep_tokens)
# ---------------------------------------------------------------------------
def t_eta_slip(rng, vessel, when):
    old = when + timedelta(hours=rng.randint(4, 10))
    new = old + timedelta(minutes=rng.choice([90, 120, 150, 180, 240, 255, 300]))
    vname = name_variant(rng, vessel)
    voy = voyage_variant(rng, vessel["voyage"])
    text = (
        f"URGENT // {vname} v.{voy} — SIN eta now {fmt_dm(new)} approx {fmt_hm(new)} LT "
        f"vice {fmt_hm(old)} LT, ETB shifted accordingly. pls adv yard side. rgds ops"
    )
    gt = {"vessel_name_canonical": vessel["name"], "voyage": vessel["voyage"],
          "previous_eta": old.isoformat(), "new_eta": new.isoformat(),
          "eta_drift_minutes": (new - old).total_seconds() / 60.0,
          "cutoff": None, "rotation_change": None, "ais_eta": None}
    return text, gt, ["vessel_name_variant", "voyage_code_confusion"], [vname, voy, fmt_hm(new), fmt_hm(old)]


def t_ais_contradiction(rng, vessel, when):
    adv_eta = when + timedelta(hours=rng.randint(5, 12))
    ais_eta = adv_eta - timedelta(minutes=rng.choice([12, 25, 40, 55, 75]))
    vname = name_variant(rng, vessel)
    text = (
        f"fyi {vname} master advises eta SIN pilot stn {fmt_dm(adv_eta)} {fmt_hm(adv_eta)} hrs. "
        f"NB our tracking shows her earlier than that — pls verify w/ AIS before replanning. "
        f"voyage ref {voyage_variant(rng, vessel['voyage'])}."
    )
    gt = {"vessel_name_canonical": vessel["name"], "voyage": vessel["voyage"],
          "previous_eta": None, "new_eta": adv_eta.isoformat(), "eta_drift_minutes": None,
          "cutoff": None, "rotation_change": None, "ais_eta": ais_eta.isoformat()}
    return text, gt, ["vessel_name_variant", "contradiction_vs_ais", "voyage_code_confusion"], [vname, fmt_hm(adv_eta)]


def t_rotation(rng, vessel, when):
    port = rng.choice(ROTATION_PORTS)
    vname = name_variant(rng, vessel)
    voy = voyage_variant(rng, vessel["voyage"])
    text = (
        f"schedule note: {vname} V.{voy} — rotation may drop {port} next call, TBC by HQ. "
        f"if {port} omitted, SIN t/s volumes shift +1 day. no firm word yet, will adv."
    )
    gt = {"vessel_name_canonical": vessel["name"], "voyage": vessel["voyage"],
          "previous_eta": None, "new_eta": None, "eta_drift_minutes": None, "cutoff": None,
          "rotation_change": {"type": "POSSIBLE_OMISSION", "port": port, "asserted": False},
          "ais_eta": None}
    return text, gt, ["vessel_name_variant", "partial_rotation", "voyage_code_confusion"], [vname, port]


def t_ambiguous_cutoff(rng, vessel, when):
    cut = when.replace(hour=rng.choice([2, 3, 4]), minute=rng.choice([0, 26, 30])) + timedelta(days=1)
    alt = cut + timedelta(hours=rng.choice([1, 2]))
    vname = name_variant(rng, vessel)
    text = (
        f"pls adv t/s ex inbound to {vname} V.{vessel['voyage']} — cutoff unchanged "
        f"{fmt_dm(cut)} {fmt_hm(cut)} hrs?? some lists show {fmt_hm(alt)}. docs cutoff vs "
        f"cargo cutoff also unclear our side. pls confirm which applies."
    )
    gt = {"vessel_name_canonical": vessel["name"], "voyage": vessel["voyage"],
          "previous_eta": None, "new_eta": None, "eta_drift_minutes": None,
          "cutoff": cut.isoformat(), "rotation_change": None, "ais_eta": None,
          "cutoff_ambiguous_alternative": alt.isoformat()}
    return text, gt, ["vessel_name_variant", "ambiguous_cutoff"], [vname, fmt_hm(cut), fmt_hm(alt)]


def t_held_no_eta(rng, vessel, when):
    port = rng.choice(ROTATION_PORTS)
    vname = name_variant(rng, vessel)
    voy = voyage_variant(rng, vessel["voyage"])
    boxes = rng.randint(6, 40)
    text = (
        f"fyi - {vname} (some lists show '{vessel['code']} {voy}') held ex {port} w/ engine "
        f"issue, carries approx {boxes} t/s bxs for SIN. no firm ETA yet, agency will adv. "
        f"pls hold planning. NB not sure if voyage is {voy} or 0{vessel['voyage']} in your system."
    )
    gt = {"vessel_name_canonical": vessel["name"], "voyage": vessel["voyage"],
          "previous_eta": None, "new_eta": None, "eta_drift_minutes": None, "cutoff": None,
          "rotation_change": None, "ais_eta": None, "box_count_approx": boxes}
    return text, gt, ["vessel_name_variant", "voyage_code_confusion"], [vname, port, str(boxes)]


def t_kitchen_sink(rng, vessel, when):
    old = when + timedelta(hours=rng.randint(6, 9))
    new = old + timedelta(minutes=rng.choice([180, 240, 255]))
    ais_eta = new - timedelta(minutes=rng.choice([10, 12, 18]))
    cut = when.replace(hour=2, minute=26) + timedelta(days=1)
    port = rng.choice(ROTATION_PORTS)
    vname = name_variant(rng, vessel)
    voy = voyage_variant(rng, vessel["voyage"])
    text = (
        f"URGENT // {vname} v.{voy} — SIN eta now {fmt_dm(new)} approx {fmt_hm(new)} LT vice "
        f"{fmt_hm(old)} LT, ETB shifted. cutoff unchanged {fmt_dm(cut)} {fmt_hm(cut)} hrs?? "
        f"Note rotation may drop {port} next call, TBC. Rgds, SG ops desk"
    )
    gt = {"vessel_name_canonical": vessel["name"], "voyage": vessel["voyage"],
          "previous_eta": old.isoformat(), "new_eta": new.isoformat(),
          "eta_drift_minutes": (new - old).total_seconds() / 60.0,
          "cutoff": cut.isoformat(),
          "rotation_change": {"type": "POSSIBLE_OMISSION", "port": port, "asserted": False},
          "ais_eta": ais_eta.isoformat()}
    classes = ["vessel_name_variant", "voyage_code_confusion", "partial_rotation",
               "contradiction_vs_ais", "ambiguous_cutoff"]
    return text, gt, classes, [vname, voy, fmt_hm(new), fmt_hm(old), fmt_hm(cut), port]


TEMPLATES = [
    ("eta_slip", t_eta_slip),
    ("ais_contradiction", t_ais_contradiction),
    ("rotation", t_rotation),
    ("ambiguous_cutoff", t_ambiguous_cutoff),
    ("held_no_eta", t_held_no_eta),
    ("kitchen_sink", t_kitchen_sink),
]


# ---------------------------------------------------------------------------
# optional local-LLM paraphrase (cached; templates alone suffice without it)
# ---------------------------------------------------------------------------
def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cache(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(path: str, cache: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _ollama_paraphrase(text: str) -> str | None:
    prompt = (
        "Rewrite the following port-operations email so it reads like a different rushed "
        "human ops clerk wrote it. STRICT RULES: keep every vessel name, voyage code, port "
        "code, number, date and time EXACTLY as written, character for character; do not add "
        "or remove facts; keep it one short paragraph; keep the messy telex/email tone. "
        "Output ONLY the rewritten text.\n\n" + text
    )
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "seed": SEED, "num_predict": 200},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None
    return (out.get("response") or "").strip() or None


def paraphrase(text: str, must_keep: list[str], cache: dict, use_llm: bool) -> tuple[str, bool]:
    """Return (final_text, was_paraphrased). Cache-first; token-validated; safe fallback."""
    key = _cache_key(text)
    if key in cache:
        cached = cache[key]
        return cached["text"], cached["paraphrased"]
    result_text, was = text, False
    if use_llm:
        candidate = _ollama_paraphrase(text)
        if candidate and all(tok in candidate for tok in must_keep):
            result_text, was = candidate, True
    cache[key] = {"text": result_text, "paraphrased": was, "template_sha256": key}
    return result_text, was


# ---------------------------------------------------------------------------
# batch generation
# ---------------------------------------------------------------------------
def generate(count: int = DEFAULT_COUNT, cache_path: str = DEFAULT_CACHE,
             use_llm: bool = True) -> dict:
    if os.environ.get("RELAY_NO_OLLAMA") == "1":
        use_llm = False
    rng = random.Random(SEED)
    cache = load_cache(cache_path)
    records = []
    for i in range(count):
        template_id, fn = TEMPLATES[i % len(TEMPLATES)]  # round-robin => class coverage
        vessel = rng.choice(FLEET)
        when = BASE_DAY + timedelta(minutes=rng.randint(0, 16 * 60))
        text, gt, classes, must_keep = fn(rng, vessel, when)
        final_text, was_paraphrased = paraphrase(text, must_keep, cache, use_llm)
        received = when + timedelta(minutes=rng.randint(1, 45))
        records.append({
            "advisory_id": f"ADV-SYN-20260825-{i + 1:03d}",
            "received_at": received.isoformat(),
            "source": rng.choice(SOURCES),
            "free_text": final_text,
            "messiness_classes": sorted(classes),
            "data_provenance": "SYNTHETIC",
            "generator": {
                "template_id": template_id,
                "seed": SEED,
                "paraphrased": was_paraphrased,
                "paraphrase_model": OLLAMA_MODEL if was_paraphrased else None,
                "template_sha256": _cache_key(text),
            },
            "ground_truth": gt,
        })
    save_cache(cache_path, cache)
    return {
        "advisories_schema_version": "1.0.0",
        "label": "SYNTHETIC",
        "_note": (
            "All advisories are SYNTHETIC (SPEC CON-5): fictional vessels/voyages, "
            "structurally faithful to the CONTRACT §a7 unstructured channel. "
            "ground_truth/messiness_classes are eval-side annotations; the fusion node "
            "receives ONLY {advisory_id, received_at, source, free_text}."
        ),
        "generator": {"seed": SEED, "count": count, "templates": [t for t, _ in TEMPLATES],
                      "messiness_classes": MESSINESS_CLASSES,
                      "paraphrase_cache": os.path.basename(DEFAULT_CACHE)},
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--no-llm", action="store_true", help="template-only; no Ollama HTTP")
    args = ap.parse_args(argv)
    doc = generate(count=args.count, cache_path=args.cache, use_llm=not args.no_llm)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    n_para = sum(1 for r in doc["records"] if r["generator"]["paraphrased"])
    print(f"{args.out}: {len(doc['records'])} advisories "
          f"({n_para} LLM-paraphrased, {len(doc['records']) - n_para} template-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
