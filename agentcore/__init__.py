"""agentcore: RELAY decision graph (LangGraph) per docs/CONTRACT.md §j.

Walking skeleton: the thinnest end-to-end path over the REAL stubs
(stubs/ is the runnable form of the contract). LLM tier is STUBBED for
determinism (fusion_stub is the canned oracle); everything else, twin,
portnet write gate, approval server, policy enforcer, ledger, is exercised
for real. See agentcore/skeleton.py (graph) and agentcore/run_skeleton.py
(the 3x-identical-digest + deny-by-default harness).
"""
