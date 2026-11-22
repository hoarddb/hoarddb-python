# SPDX-License-Identifier: Apache-2.0
"""P1: the 22-byte frame codec (wire.md section 2; spec 5.3)."""

from __future__ import annotations

import random
import tracemalloc

import pytest

import vectors
from hoarddb import errors
from hoarddb._core import frame

FRAME_VECTORS = vectors.driver_vectors(kind="frame")
ACCEPT = [v for v in FRAME_VECTORS if v["expect"]["result"] == "accept"]
REFUSE = [v for v in FRAME_VECTORS if v["expect"]["result"] == "refuse"]
RID = bytes(range(16))


def _feed_bytewise(decoder: frame.FrameDecoder, raw: bytes) -> list[frame.Frame]:
    got: list[frame.Frame] = []
    for i in range(len(raw)):
        got.extend(decoder.feed(raw[i : i + 1]))
    return got


def _refusal_reason(blob: bytes) -> str | None:
    try:
        for f in frame.FrameDecoder().feed(blob):
            assert 17 + len(f.payload) <= frame.MAX_LENGTH
    except frame.FrameRefusal as refusal:
        return refusal.reason
    return None


def test_the_vector_file_holds_the_seven_frame_vectors() -> None:
    assert (len(ACCEPT), len(REFUSE)) == (3, 4)


def test_constants_are_the_wire_constants() -> None:
    assert frame.HEADER_SIZE == 22
    assert frame.PREFIX_SIZE == 5
    assert frame.VERSION == 1
    assert frame.MIN_LENGTH == 17
    assert frame.MAX_LENGTH == 33_554_432


@pytest.mark.parametrize("vec", ACCEPT, ids=lambda v: v["name"])
def test_accepted_vectors_decode_to_exactly_the_stated_fields(vec: vectors.Vector) -> None:
    expect = vec["expect"]
    decoded = frame.decode_frame(bytes.fromhex(vec["frame_hex"]))
    assert decoded.opcode == expect["opcode"]
    assert decoded.request_id.hex() == expect["request_id_hex"]
    assert decoded.payload.hex() == expect.get("payload_hex", "")
    assert 17 + len(decoded.payload) == expect["length"]


@pytest.mark.parametrize("vec", ACCEPT, ids=lambda v: v["name"])
def test_accepted_vectors_re_encode_byte_for_byte(vec: vectors.Vector) -> None:
    expect = vec["expect"]
    encoded = frame.encode_frame(
        expect["opcode"],
        bytes.fromhex(expect["request_id_hex"]),
        bytes.fromhex(expect.get("payload_hex", "")),
    )
    assert encoded.hex() == vec["frame_hex"]


@pytest.mark.parametrize("vec", REFUSE, ids=lambda v: v["name"])
def test_refused_vectors_are_refused_for_the_stated_reason(vec: vectors.Vector) -> None:
    reason = vec["expect"]["refuse_because"]
    with pytest.raises(frame.FrameRefusal) as info:
        frame.decode_frame(bytes.fromhex(vec["frame_hex"]))
    assert info.value.reason == reason


@pytest.mark.parametrize("vec", FRAME_VECTORS, ids=lambda v: v["name"])
def test_the_incremental_decoder_agrees_with_the_vectors_one_byte_at_a_time(
    vec: vectors.Vector,
) -> None:
    raw = bytes.fromhex(vec["frame_hex"])
    decoder = frame.FrameDecoder()
    expect = vec["expect"]
    if expect["result"] == "refuse" and expect["refuse_because"] != "truncated":
        with pytest.raises(frame.FrameRefusal) as info:
            _feed_bytewise(decoder, raw)
        assert info.value.reason == expect["refuse_because"]
        return
    got = _feed_bytewise(decoder, raw)
    if expect["result"] == "accept":
        assert [f.opcode for f in got] == [expect["opcode"]]
        assert decoder.at_boundary
    else:
        assert got == []
        assert not decoder.at_boundary  # EOF here would be a truncated frame


def test_the_header_is_validated_before_the_body_exists() -> None:
    for prefix, reason in [
        (b"\x01\xff\xff\xff\xff", "length-too-large"),
        (b"\x01\x10\x00\x00\x00", "length-too-small"),
        (b"\x01\x01\x00\x00\x02", "length-too-large"),
        (b"\x02\x11\x00\x00\x00", "bad-version"),
        (b"\x00\x11\x00\x00\x00", "bad-version"),
    ]:
        with pytest.raises(frame.FrameRefusal) as info:
            frame.parse_prefix(prefix)
        assert info.value.reason == reason


def test_a_version_error_wins_over_a_length_error() -> None:
    with pytest.raises(frame.FrameRefusal) as info:
        frame.parse_prefix(b"\x07\xff\xff\xff\xff")
    assert info.value.reason == "bad-version"


def test_the_limits_are_inclusive() -> None:
    assert frame.parse_prefix(b"\x01\x11\x00\x00\x00") == 17
    assert frame.parse_prefix(b"\x01\x00\x00\x00\x02") == 33_554_432


def test_a_hostile_length_allocates_nothing() -> None:
    tracemalloc.start()
    try:
        for hostile in (b"\x01\xff\xff\xff\xff", b"\x01\x10\x00\x00\x00"):
            decoder = frame.FrameDecoder()
            tracemalloc.reset_peak()
            with pytest.raises(frame.FrameRefusal):
                decoder.feed(hostile)
            _, peak = tracemalloc.get_traced_memory()
            assert peak < 64 * 1024, f"allocated {peak} bytes before refusing"
    finally:
        tracemalloc.stop()


def test_a_refused_decoder_stays_refused() -> None:
    decoder = frame.FrameDecoder()
    with pytest.raises(frame.FrameRefusal) as first:
        decoder.feed(b"\x02\x11\x00\x00\x00")
    with pytest.raises(frame.FrameRefusal) as again:
        decoder.feed(b"\x01\x11\x00\x00\x00" + bytes(17))
    assert again.value.reason == first.value.reason


def test_two_frames_in_one_chunk_and_a_frame_split_anywhere() -> None:
    a = frame.encode_frame(0x10, RID, b"abc")
    b = frame.encode_frame(0xA0, bytes(16), b"")
    stream = a + b
    whole = frame.FrameDecoder().feed(stream)
    assert [(f.opcode, f.payload) for f in whole] == [(0x10, b"abc"), (0xA0, b"")]
    for cut in range(len(stream) + 1):
        decoder = frame.FrameDecoder()
        got = decoder.feed(stream[:cut]) + decoder.feed(stream[cut:])
        assert got == whole


def test_recv_into_style_use_fills_a_presized_buffer() -> None:
    decoder = frame.FrameDecoder()
    raw = frame.encode_frame(0x11, RID, b"x" * 100)
    pos = 0
    out: frame.Frame | None = None
    while out is None:
        view = decoder.next_buffer()
        n = min(len(view), 7, len(raw) - pos)  # a short read
        view[:n] = raw[pos : pos + n]
        pos += n
        out = decoder.commit(n)
    assert (out.opcode, out.request_id, out.payload) == (0x11, RID, b"x" * 100)
    assert pos == len(raw)
    assert len(decoder.next_buffer()) == frame.PREFIX_SIZE


def test_the_body_buffer_is_sized_exactly_once_the_prefix_is_valid() -> None:
    decoder = frame.FrameDecoder()
    view = decoder.next_buffer()
    view[:5] = b"\x01\x31\x00\x00\x00"  # length 49
    assert decoder.commit(5) is None
    assert len(decoder.next_buffer()) == 49


def test_encode_refuses_an_oversize_frame_before_building_anything() -> None:
    assert len(frame.encode_header(0x10, RID, frame.MAX_LENGTH - 17)) == frame.HEADER_SIZE
    with pytest.raises(frame.FrameTooLarge) as info:
        frame.encode_header(0x10, RID, frame.MAX_LENGTH - 16)
    assert info.value.size == frame.MAX_LENGTH + 1
    assert info.value.limit == frame.MAX_LENGTH


def test_encode_rejects_a_malformed_request_id_and_opcode() -> None:
    with pytest.raises(errors.InvalidArgumentError, match="request id"):
        frame.encode_frame(0x10, b"short", b"")
    with pytest.raises(errors.InvalidArgumentError, match="opcode"):
        frame.encode_frame(256, RID, b"")


@pytest.mark.parametrize("length", [-1, -(2**40), True, 1.5, "3", None])
def test_encode_header_rejects_a_payload_length_that_is_not_a_size(length: object) -> None:
    # a negative length used to slip under the minimum and write a frame no peer accepts
    with pytest.raises(errors.InvalidArgumentError, match="payload length"):
        frame.encode_header(0x10, RID, length)  # type: ignore[arg-type]


@pytest.mark.parametrize("opcode", [-1, True, 1.0, None])
def test_encode_header_rejects_an_opcode_that_is_not_a_byte(opcode: object) -> None:
    with pytest.raises(errors.InvalidArgumentError, match="opcode"):
        frame.encode_header(opcode, RID, 0)  # type: ignore[arg-type]


def test_request_ids_are_sixteen_random_bytes() -> None:
    ids = {frame.new_request_id() for _ in range(64)}
    assert len(ids) == 64
    assert all(len(i) == 16 for i in ids)


def test_fuzz_arbitrary_bytes_only_ever_raise_a_frame_refusal() -> None:
    rng = random.Random(0xF4A3E)
    for _ in range(3000):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 80)))
        if rng.random() < 0.5 and blob:
            blob = b"\x01" + blob[1:]
        assert _refusal_reason(blob) in {
            None,
            "bad-version",
            "length-too-small",
            "length-too-large",
        }


def test_fuzz_encode_decode_round_trip() -> None:
    rng = random.Random(7)
    for _ in range(300):
        payload = bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 300)))
        rid = bytes(rng.getrandbits(8) for _ in range(16))
        opcode = rng.randrange(256)
        decoded = frame.decode_frame(frame.encode_frame(opcode, rid, payload))
        assert (decoded.opcode, decoded.request_id, decoded.payload) == (opcode, rid, payload)


def _via_buffers(blob: bytes) -> tuple[list[frame.Frame], str | None]:
    """The frames and the refusal (if any) of ``blob`` through next_buffer/commit, byte by byte."""
    decoder = frame.FrameDecoder()
    got: list[frame.Frame] = []
    try:
        for byte in blob:
            decoder.next_buffer()[:1] = bytes([byte])
            done = decoder.commit(1)
            if done is not None:
                got.append(done)
    except frame.FrameRefusal as refusal:
        return got, refusal.reason
    return got, None


def _via_feed(blob: bytes) -> tuple[list[frame.Frame], str | None]:
    """The same through one ``feed`` call; a refusal after frames shows on the next call."""
    decoder = frame.FrameDecoder()
    try:
        got = decoder.feed(blob)
    except frame.FrameRefusal as refusal:
        return [], refusal.reason
    try:
        decoder.feed(b"")
    except frame.FrameRefusal as refusal:
        return got, refusal.reason
    return got, None


def test_a_valid_frame_followed_by_garbage_in_one_chunk_is_delivered_then_refused() -> None:
    good = frame.encode_frame(0x10, RID, b"answer")
    decoder = frame.FrameDecoder()
    got = decoder.feed(good + b"\x09\xff\xff\xff\xff")
    assert [(f.opcode, f.payload) for f in got] == [(0x10, b"answer")]
    with pytest.raises(frame.FrameRefusal) as refused:
        decoder.feed(b"")
    assert refused.value.reason == "bad-version"


def test_feed_and_next_buffer_commit_agree_on_streams() -> None:
    rng = random.Random(0xA11CE)
    for _ in range(2000):
        blob = b""
        for _ in range(rng.randrange(0, 4)):
            payload = bytes(rng.getrandbits(8) for _ in range(rng.randrange(0, 20)))
            blob += frame.encode_frame(rng.randrange(256), RID, payload)
        if rng.random() < 0.6:
            blob += bytes(rng.getrandbits(8) for _ in range(rng.randrange(1, 12)))
        if rng.random() < 0.3 and blob:
            blob = blob[: rng.randrange(len(blob) + 1)]
        assert _via_feed(blob) == _via_buffers(blob), blob.hex()
