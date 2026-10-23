"""G.711 codec tables: reference values, symmetry, monotonicity, round-trip error bounds."""

import numpy as np
import pytest

from calls.audio.g711 import (
    alaw_decode,
    alaw_decode_scalar,
    alaw_encode,
    alaw_encode_scalar,
    ulaw_decode,
    ulaw_decode_scalar,
    ulaw_encode,
    ulaw_encode_scalar,
)


@pytest.mark.parametrize(
    "code,expected",
    [(0xFF, 0), (0x7F, 0), (0x80, 32124), (0x00, -32124), (0xFE, 8), (0x7E, -8)],
)
def test_ulaw_reference_points(code, expected):
    assert ulaw_decode_scalar(code) == expected
    assert int(ulaw_decode(bytes([code]))[0]) == expected


@pytest.mark.parametrize(
    "code,expected",
    [(0xD5, 8), (0x55, -8), (0xAA, 32256), (0x2A, -32256), (0xD4, 24), (0x54, -24)],
)
def test_alaw_reference_points(code, expected):
    assert alaw_decode_scalar(code) == expected
    assert int(alaw_decode(bytes([code]))[0]) == expected


def test_tables_match_scalar_reference():
    codes = np.arange(256, dtype=np.uint8)
    assert np.array_equal(ulaw_decode(codes), [ulaw_decode_scalar(c) for c in range(256)])
    assert np.array_equal(alaw_decode(codes), [alaw_decode_scalar(c) for c in range(256)])
    pcm = np.arange(-32768, 32768, 97, dtype=np.int16)
    assert np.array_equal(ulaw_encode(pcm), [ulaw_encode_scalar(int(v)) for v in pcm])
    assert np.array_equal(alaw_encode(pcm), [alaw_encode_scalar(int(v)) for v in pcm])


def test_decoders_are_odd_symmetric():
    for code in range(128):
        assert ulaw_decode_scalar(code) == -ulaw_decode_scalar(code | 0x80)
        assert alaw_decode_scalar(code) == -alaw_decode_scalar(code | 0x80)


@pytest.mark.parametrize("enc,dec", [(ulaw_encode, ulaw_decode), (alaw_encode, alaw_decode)])
def test_round_trip_is_monotonic_and_bounded(enc, dec):
    pcm = np.arange(-32768, 32768, dtype=np.int16)
    x = pcm.astype(np.int64)
    rt = dec(enc(pcm)).astype(np.int64)
    assert np.all(np.diff(rt) >= 0), "decode(encode(x)) must be non-decreasing in x"
    err = np.abs(rt - x)
    # overload: the extremes map to the largest code words
    top = 32124 if enc is ulaw_encode else 32256
    assert rt[0] == -top and rt[-1] == top
    # inside the overload point the error is bounded by half the top-segment step
    # (1024 in 16-bit units for both laws) plus the discarded low input bits
    inside = np.abs(x) <= top
    bound = 512 + (3 if enc is ulaw_encode else 7)
    assert int(err[inside].max()) <= bound
    # small signals: one quantisation step near zero (8 μ-law / 16 A-law in 16-bit units;
    # the reference encoders truncate rather than round) plus the discarded low input bits
    small = np.abs(x) < 256
    assert int(err[small].max()) <= (8 + 3 if enc is ulaw_encode else 16 + 7)


def test_codec_round_trip_of_decoded_codes_is_identity():
    codes = np.arange(256, dtype=np.uint8)
    u = ulaw_encode(ulaw_decode(codes))
    assert u[0x7F] == 0xFF  # μ-law has two zero codes; the encoder emits the positive one
    assert np.array_equal(np.delete(u, 0x7F), np.delete(codes, 0x7F))
    assert np.array_equal(alaw_encode(alaw_decode(codes)), codes)


def test_bytes_input_and_dtype():
    out = alaw_decode(b"\xd5\x55")
    assert out.dtype == np.int16 and out.tolist() == [8, -8]
    enc = ulaw_encode(np.array([0, 8, -8], dtype=np.int16))
    assert enc.dtype == np.uint8
