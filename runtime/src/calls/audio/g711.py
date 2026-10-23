"""ITU-T G.711 A-law / μ-law codecs (integer, table driven, deterministic).

Decoding follows the reference segment/mantissa expansion; encoding follows the
reference segment search (as in the classic Sun ``g711.c``). Both directions are
exposed as NumPy table lookups so that a 20 ms frame is converted with one indexing
operation and the result is bit-identical on every platform.
"""

from __future__ import annotations

import numpy as np

_SIGN_BIT = 0x80
_QUANT_MASK = 0x0F
_SEG_SHIFT = 4
_SEG_MASK = 0x70
_ULAW_BIAS = 0x84
_ULAW_CLIP = 8159

_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)
_SEG_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _search(val: int, table: tuple[int, ...]) -> int:
    for i, limit in enumerate(table):
        if val <= limit:
            return i
    return len(table)


def alaw_decode_scalar(a_val: int) -> int:
    a_val ^= 0x55
    t = (a_val & _QUANT_MASK) << 4
    seg = (a_val & _SEG_MASK) >> _SEG_SHIFT
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (a_val & _SIGN_BIT) else -t


def ulaw_decode_scalar(u_val: int) -> int:
    u_val = ~u_val & 0xFF
    t = ((u_val & _QUANT_MASK) << 3) + _ULAW_BIAS
    t <<= (u_val & _SEG_MASK) >> _SEG_SHIFT
    return (_ULAW_BIAS - t) if (u_val & _SIGN_BIT) else (t - _ULAW_BIAS)


def alaw_encode_scalar(pcm_val: int) -> int:
    pcm_val = pcm_val >> 3  # 16-bit -> 13-bit
    if pcm_val >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm_val = -pcm_val - 1
    seg = _search(pcm_val, _SEG_AEND)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << _SEG_SHIFT
    if seg < 2:
        aval |= (pcm_val >> 1) & _QUANT_MASK
    else:
        aval |= (pcm_val >> seg) & _QUANT_MASK
    return aval ^ mask


def ulaw_encode_scalar(pcm_val: int) -> int:
    pcm_val = pcm_val >> 2  # 16-bit -> 14-bit
    if pcm_val < 0:
        pcm_val = -pcm_val
        mask = 0x7F
    else:
        mask = 0xFF
    if pcm_val > _ULAW_CLIP:
        pcm_val = _ULAW_CLIP
    pcm_val += _ULAW_BIAS >> 2
    seg = _search(pcm_val, _SEG_UEND)
    if seg >= 8:
        return 0x7F ^ mask
    uval = (seg << 4) | ((pcm_val >> (seg + 1)) & 0x0F)
    return uval ^ mask


def _build_tables() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    a_dec = np.array([alaw_decode_scalar(i) for i in range(256)], dtype=np.int16)
    u_dec = np.array([ulaw_decode_scalar(i) for i in range(256)], dtype=np.int16)
    lin = np.arange(-32768, 32768, dtype=np.int64)
    a_enc = np.array([alaw_encode_scalar(int(v)) for v in lin], dtype=np.uint8)
    u_enc = np.array([ulaw_encode_scalar(int(v)) for v in lin], dtype=np.uint8)
    return a_dec, u_dec, a_enc, u_enc


ALAW_DECODE_TABLE, ULAW_DECODE_TABLE, _ALAW_ENCODE_TABLE, _ULAW_ENCODE_TABLE = _build_tables()


def _as_u8(data) -> np.ndarray:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return np.frombuffer(data, dtype=np.uint8)
    return np.asarray(data, dtype=np.uint8)


def alaw_decode(data) -> np.ndarray:
    """G.711 A-law bytes -> int16 PCM."""
    return ALAW_DECODE_TABLE[_as_u8(data)]


def ulaw_decode(data) -> np.ndarray:
    """G.711 μ-law bytes -> int16 PCM."""
    return ULAW_DECODE_TABLE[_as_u8(data)]


def alaw_encode(pcm) -> np.ndarray:
    """int16 PCM -> G.711 A-law bytes (uint8 array)."""
    x = np.asarray(pcm, dtype=np.int32)
    return _ALAW_ENCODE_TABLE[x + 32768]


def ulaw_encode(pcm) -> np.ndarray:
    """int16 PCM -> G.711 μ-law bytes (uint8 array)."""
    x = np.asarray(pcm, dtype=np.int32)
    return _ULAW_ENCODE_TABLE[x + 32768]
