"""Bridge between the research runtime and the evolution engine (M1-5A).

Loads research-session extraction graphs into the evolution repository and
derives ChangeEvents from the real corpus version history, so the P0
evolution engine operates on a graph grounded in actual documents instead
of hand-authored fixtures.
"""

from veritas.integration.graph_bridge import GraphBridge, GraphBridgeError

__all__ = ["GraphBridge", "GraphBridgeError"]
