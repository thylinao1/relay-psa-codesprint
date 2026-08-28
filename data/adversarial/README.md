# Adversarial advisory dataset

`advisories_adversarial.jsonl` holds 48 SYNTHETIC adversarial carrier advisories used to
measure the fusion node's robustness and its resistance to prompt injection. Every record is
labelled `SYNTHETIC` and uses the CONTRACT §a7 unstructured-channel shape
(`advisory_id`, `received_at`, `source`, `free_text`) plus eval-side annotations the fusion
node never receives.

## Purpose

The advisory free text is untrusted data, not instruction (CONTRACT §e). This dataset stresses
that boundary. It feeds two things:

1. `evalx/fusion_eval.py --ladder`, which scores the fusion tiers (regex baseline, local
   llama3.2:3b, local llama3.1:8b) over a corpus of at least 200 advisories and measures
   injection resistance through the real decision graph.
2. `agentcore/tests/test_fusion_adversarial.py`, which asserts the structural agency boundary
   and the through-graph injection resistance.

## Classes

| class | count | what it tests |
|---|---|---|
| prompt_injection | 12 | instructions embedded in the advisory (ignore-previous, approve-everything, call a named tool, exfiltrate a token, fake system prompts, tool-call syntax) |
| fabrication_bait | 10 | plausible but false vessels and voyages that are not in the twin world; the reconciliation layer must refuse to match them |
| contradiction_trap | 8 | internally inconsistent advisories (two firm ETAs, cut-off before berth, an advance labelled a delay, a drift that does not recompute) |
| malformed | 8 | empty, whitespace, word salad, HTML, SQL payloads, and truncated fragments with no extractable fact |
| oversized | 2 | very long text with a fact buried far in, or none at all |
| unicode_trick | 8 | homoglyph vessel names, zero-width joiners, right-to-left override, fullwidth digits, combining diacritics, emoji noise |

## Record fields (eval-side annotations)

- `adversarial_class`: one of the classes above.
- `expected`: `must_not_write` (always true), `reconciles_to` (connection ids a valid record
  should map to, empty when it should not reconcile), `must_escalate`.
- `injection_markers`: tokens from the injection payload that must never surface as a fact
  decision value (voyage, cut-off, connection).
- `ground_truth`: canonical vessel name where a real vessel is referenced, `in_world`,
  `expected_new_eta`.

Some prompt-injection records carry a real MERLION EXPRESS 437W ETA and therefore reconcile
legitimately to a connection. That is deliberate: it lets the eval show that the injection
payload is inert even when the surrounding advisory is otherwise valid.

## Regenerate

```
python3 data/adversarial/generate_adversarial.py
```

Deterministic, no network. The generator prints class coverage on completion.
