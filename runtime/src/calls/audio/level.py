"""Level calibration in dBm0 for 16-bit linear PCM.

Convention (ITU-T G.711): a full-scale sine in the 16-bit linear domain corresponds to
+3.14 dBm0 (A-law overload point; μ-law is +3.17 dBm0, the 0.03 dB difference is ignored).
All DTMF level criteria in ITU-T Q.24 are expressed per frequency in dBm0, so every
detector threshold is converted through these two functions and nowhere else.
"""

import math

FULL_SCALE = 32768.0
FULL_SCALE_DBM0 = 3.14


def dbm0_to_amplitude(dbm0: float) -> float:
    """Peak amplitude (16-bit units) of a sine whose level is ``dbm0``."""
    return FULL_SCALE * 10.0 ** ((dbm0 - FULL_SCALE_DBM0) / 20.0)


def amplitude_to_dbm0(amplitude: float) -> float:
    """Level in dBm0 of a sine with peak ``amplitude`` (16-bit units)."""
    if amplitude <= 0.0:
        return -math.inf
    return 20.0 * math.log10(amplitude / FULL_SCALE) + FULL_SCALE_DBM0
