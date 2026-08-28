"""TerminalTwin: SimPy terminal model behind the generated worlds.

Models the transhipment processing chain per box group as a discrete-event
pipeline: quay cranes discharge at the berth, then yard cranes (aRMG) move the
group into its block while COMPETING with background yard traffic whose
intensity scales with block density. The P90 buffer the feasibility engine
consumes (`buffer_p90_minutes`) is derived EMPIRICALLY here: P90 minus median
of the simulated chain across seeded replications, the pain is the P90
buffer, not the average wait (SPEC §1; calibration sources in
twin/CALIBRATION.md).

Everything is seeded: two constructions with the same world + seed produce
byte-identical samples. No wall clock anywhere.
"""

from __future__ import annotations

import math
import random
import statistics

import simpy

import twin  # noqa: F401  (sys.path setup)

# Calibrated constants: sources and reasoning in twin/CALIBRATION.md.
ARMG_PER_BLOCK = 2                     # yard cranes serving one block
BASE_MOVE_MINUTES = 2.1                # one aRMG box move, uncontended
MOVE_TIME_SIGMA = 0.35                 # lognormal sigma of a single move
BACKGROUND_JOBS_PER_HOUR_AT_80 = 10.0  # competing yard jobs at 80% density
DENSITY_TRAFFIC_SLOPE = 1.25           # extra jobs/h per density point above 80
BACKGROUND_JOB_MOVES = (4, 14)         # moves per competing background job
REPLICATIONS_DEFAULT = 120             # seeded Monte-Carlo replications
SIM_HORIZON_MINUTES = 24 * 60.0

# lognormal mu such that the MEDIAN single-move time equals BASE_MOVE_MINUTES
_MU_MOVE = math.log(BASE_MOVE_MINUTES)


def _background_rate_per_min(density_pct: float) -> float:
    """Competing yard-job arrival rate for a block at a given density."""
    excess = max(0.0, density_pct - 80.0)
    per_hour = BACKGROUND_JOBS_PER_HOUR_AT_80 + DENSITY_TRAFFIC_SLOPE * excess
    return per_hour / 60.0


class TerminalTwin:
    """Discrete-event twin of one terminal world (world.json schema)."""

    def __init__(self, world: dict, seed: int = 42):
        self.world = world
        self.seed = seed
        self._blocks = {b["block_id"]: b for b in world["yard_state"]["blocks"]}
        self._box_groups = {bg["box_group_id"]: bg for bg in world["box_groups"]}

    # ------------------------------------------------------------------
    # one replication of the yard-transfer leg for one box group
    # ------------------------------------------------------------------
    def _one_transfer(self, rng: random.Random, block_id: str, box_count: int) -> float:
        """Simulate ONE yard transfer (minutes) of `box_count` boxes through
        the block's aRMG pool under density-scaled background contention."""
        density = float(self._blocks[block_id]["density_pct"]) if block_id in self._blocks else 80.0
        env = simpy.Environment()
        cranes = simpy.Resource(env, capacity=ARMG_PER_BLOCK)
        done_at = {"t": 0.0}

        def move_batch(n_moves: int, record: bool):
            with cranes.request() as req:
                yield req
                for _ in range(n_moves):
                    yield env.timeout(rng.lognormvariate(
                        _MU_MOVE, MOVE_TIME_SIGMA))
            if record:
                done_at["t"] = env.now

        def background(rate_per_min: float):
            while True:
                yield env.timeout(rng.expovariate(rate_per_min))
                env.process(move_batch(rng.randint(*BACKGROUND_JOB_MOVES), record=False))

        rate = _background_rate_per_min(density)
        if rate > 0:
            env.process(background(rate))
        # Our job enters after a short seeded stagger so it lands mid-traffic.
        stagger = rng.uniform(0.0, 20.0)

        def our_job():
            yield env.timeout(stagger)
            yield env.process(move_batch(box_count, record=True))

        proc = env.process(our_job())
        env.run(until=proc)   # stop once OUR chain completes (Process is an event)
        return done_at["t"] - stagger

    # ------------------------------------------------------------------
    # empirical distribution + P90 buffer
    # ------------------------------------------------------------------
    def transfer_samples(self, connection_id: str,
                         n: int = REPLICATIONS_DEFAULT) -> list[float]:
        """Seeded Monte-Carlo samples of the yard-transfer leg (minutes)."""
        conn = next(c for c in self.world["connections"]
                    if c["connection_id"] == connection_id)
        block_id = conn.get("yard_block")
        bg = self._box_groups.get(conn["box_group_id"], {})
        box_count = int(bg.get("box_count") or 12)
        samples = []
        for i in range(n):
            rng = random.Random((self.seed, connection_id, i).__repr__())
            samples.append(round(self._one_transfer(rng, block_id or "?", box_count), 3))
        return samples

    def p90_buffer(self, connection_id: str, n: int = REPLICATIONS_DEFAULT) -> float:
        """buffer_p90_minutes = P90 - median of the simulated transfer chain,
        rounded to 5 min (planning granularity). Never below 15 min."""
        samples = sorted(self.transfer_samples(connection_id, n))
        p90 = samples[min(len(samples) - 1, int(0.9 * len(samples)))]
        med = statistics.median(samples)
        buffer_min = max(15.0, p90 - med)
        return float(round(buffer_min / 5.0) * 5.0)

    def median_transfer(self, connection_id: str, n: int = REPLICATIONS_DEFAULT) -> float:
        """Median simulated transfer time (minutes), 5-min granularity."""
        med = statistics.median(self.transfer_samples(connection_id, n))
        return float(max(30.0, round(med / 5.0) * 5.0))
