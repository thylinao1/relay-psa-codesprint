"""Domain bindings for the governance core.

An adapter supplies the four things the core does not know: the policy
table, the approval card schema and wording, the availability and loop
probes, and the simulator that re-scores an edited plan. The core imports
nothing from any adapter.
"""
