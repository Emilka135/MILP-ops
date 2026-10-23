"""Segment S5 — ASR gateway contracts.

``events`` defines the engine-neutral event model (partial / stable / final) that the
dialog layer consumes; ``deeray_frontier`` maps the GigaAM v3 Runtime (Deeray) WebSocket
protocol onto it and checks the runtime's own monotonicity guarantees.
"""

from .deeray_frontier import DeerayStreamTracker, ProtocolViolation
from .events import AsrEvent, Word

__all__ = ["AsrEvent", "DeerayStreamTracker", "ProtocolViolation", "Word"]
