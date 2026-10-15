"""calls — telephony voice-interaction platform.

Package layout mirrors the segments of docs/ARCHITECTURE.md:

- calls.audio  — S2 helpers (G.711 codecs, level calibration)
- calls.dtmf   — S3 DTMF: in-band Goertzel/DFT detector (ITU-T Q.24 criteria),
                 RFC 4733 telephone-event parser, source arbiter, digit collector
- calls.asr    — S5 ASR gateway contracts and the GigaAM v3 Runtime stream tracker
"""

__all__ = ["__version__"]
__version__ = "0.0.1"
