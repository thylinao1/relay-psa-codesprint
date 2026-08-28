# Third-party dependencies, data sources and terms

This file lists every runtime dependency and external data source RELAY uses or
references, with its licence. The dependency guardrail is stated in
`docs/CONTRACT.md`: MIT, Apache-2.0, BSD or CC0 dependencies only; GPL material
may be cited as a benchmark, never vendored. RELAY's own code is under the MIT
License (`LICENSE`).

## Runtime Python dependencies

| Package | Version | Licence | Used by |
|---|---|---|---|
| langgraph | 1.2.11 | MIT | `agentcore/graph.py` (the `relay_decision_graph` StateGraph) |
| langgraph-checkpoint-sqlite | 3.1.1 | MIT | SQLite checkpointer for interrupt and resume |
| langgraph-checkpoint / langgraph-prebuilt / langgraph-sdk / langchain-core | pinned by pip | MIT | transitive dependencies of langgraph |
| ortools | 9.15.6755 | Apache-2.0 | `twin/solver.py` (CP-SAT re-planner, seed 42, single worker) |
| simpy | 4.1.2 | MIT | `twin/world.py` (terminal twin processes) |
| requests | 2.34.2 | Apache-2.0 | `agentcore/tiers.py` (local Ollama HTTP client), data tooling |
| websockets | 16.1.1 | BSD-3-Clause | `data/ais_recorder.py` (AIS recording; not on the demo path) |
| pytest | 9.1.1 | MIT | test suite only |

Licences read from the installed distributions' metadata
(`.venv/lib/python3.13/site-packages/<pkg>-<ver>.dist-info/METADATA`).
Remaining transitive dependencies (pydantic, numpy, protobuf, orjson and
others) were spot-checked and carry MIT, BSD or Apache-2.0 terms.

## Models

- llama3.2:3b, served locally through Ollama (Ollama itself is MIT). The model
  weights are distributed by Meta under the Llama 3.2 Community License and are
  NOT included in this repository; a judge without Ollama loses only the
  optional live-fusion demonstration.
- Optional frontier tier: a named hosted provider reached over HTTPS with an
  env-var key, default OFF (`agentcore/tiers.py`). No provider SDK is vendored.

## Data sources

- **Synthetic terminal data.** All terminal state, connections, vessels, box
  groups, advisories and worlds are SYNTHETIC and labelled
  `"label": "SYNTHETIC"` (or `data_provenance: "SYNTHETIC"`) in the data
  itself. Event shapes follow the published DCSA Port Call 2.0 / JIT standard
  in structure only; no DCSA artefact is vendored.
- **aisstream.io (recorded AIS).** One event in `data/packs/disruption.json`
  carries a real recorded ETA-drift magnitude from a Singapore bounding box
  (24 Aug 2026, WebSocket API, personal API key held in the gitignored
  `.env`). aisstream.io publishes no explicit data licence; hackathon use is
  recorded as an assumption in the working repository's research notes. Raw
  recordings are gitignored and never distributed. Vessel identities are
  pseudonymised deterministically (salted SHA-256 of the MMSI,
  `data/extract_drift.py`).
- **Danish Maritime Authority (DMA) AIS files.** Documented as the
  licence-clean fallback source (published free for download under the Danish
  PSI act). No DMA data is distributed; `data/extract_drift.py` only emits a
  DMA-like column set as its table shape.
- **data.gov.sg (MPA statistics).** Container-throughput and vessel-arrival
  statistics are referenced for macro context under the Singapore Open Data
  Licence version 1.0. Attribution: contains information from data.gov.sg
  accessed via the datastore API, made available under the terms of the
  Singapore Open Data Licence version 1.0.
- **NEA real-time weather (data.gov.sg APIs).** Referenced to shape the
  synthetic `weather_alert` events (station id S117); same Singapore Open Data
  Licence version 1.0 and attribution as above. No NEA data is distributed.
- **Barcelona berth-allocation benchmark**
  (`alberto-santini/berth-allocation-problems`, GPL-3.0): cited as an external
  benchmark reference only. It is explicitly NOT distributed here; no code or
  data from it is vendored anywhere in this repository.
- **PSA Annual Report and Sustainability Report 2025.** Publicly published
  figures are quoted with page citations in the written explanation.
- **Fault taxonomy.** Nine of the ten fault type names are adapted from the
  MIT agentic-fault-diagnosis taxonomy; names only, no code vendored. The
  tenth (APPROVER_UNREACHABLE) is RELAY's own.
