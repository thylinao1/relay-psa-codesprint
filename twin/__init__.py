"""twin/: the REAL terminal twin behind the frozen twin-mcp interface.

The frozen INTERFACE stays `stubs/twin_stub.py` (CONTRACT §b1) and the frozen
fixtures stay `stubs/fixtures/`, this package replaces the INTERNALS with a
real engine and extends it beyond the fixture world:

    twin.generate:    ConFlowGen-style deterministic world generator
                        (calibrated constants in twin/CALIBRATION.md)
    twin.world:       SimPy terminal model (discharge -> yard -> quay) used
                        to derive empirical P90 buffers for generated worlds
    twin.feasibility , the ConnectionFeasibility engine (CONTRACT §b1.2):
                        completeness gate + margin arithmetic, byte-identical
                        to the stub on the frozen fixtures
    twin.solver:      CP-SAT terminal re-planner (CONTRACT §b1.3 semantics:
                        fixed seed 42, num_search_workers=1, lexicographic
                        tie-breaks, binding_constraint on every rejection)
    twin.greedy:      greedy fallback re-planner + the shipped comparison
                        subject for the CP-SAT-vs-greedy quality row
    twin.mcp_server:  CONTRACT twin-mcp tools over stdio JSON-RPC

Shared world state (the runtime overlay in stubs/world_state.json) remains the
single source of truth: this engine READS the effective world through
`stubs.load_world()` so approved writes and ingests done anywhere on the
checkout are visible here too (SPEC SIG-1, "the board recovers").

All data produced by this package is SYNTHETIC and labelled so.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PROJECT_ROOT = _ROOT
