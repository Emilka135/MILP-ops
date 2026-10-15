"""Segment S3 — DTMF: reaction to key-press tones.

- ``inband``   : deterministic in-band detector (Goertzel/DFT bank, ITU-T Q.24 criteria)
- ``rfc4733``  : RTP telephone-event (RFC 4733) parser — the out-of-band source
- ``arbiter``  : merges OOB and in-band observations into exactly-once ``DigitEvent``s
- ``collector``: ``collect_digits`` primitive with media-clock timeouts
- ``synth``    : DTMF signal generator for acceptance tests and the lab stand
"""

from .arbiter import DigitEvent, DtmfArbiter
from .collector import CollectResult, CollectSpec, DigitCollector
from .events import DigitEnd, DigitStart
from .inband import InbandDtmfDetector
from .params import DIGITS, HIGH_FREQS, LOW_FREQS, DtmfParams
from .rfc4733 import OobDigit, TelephoneEventParser

__all__ = [
    "DIGITS",
    "HIGH_FREQS",
    "LOW_FREQS",
    "CollectResult",
    "CollectSpec",
    "DigitCollector",
    "DigitEnd",
    "DigitEvent",
    "DigitStart",
    "DtmfArbiter",
    "DtmfParams",
    "InbandDtmfDetector",
    "OobDigit",
    "TelephoneEventParser",
]
