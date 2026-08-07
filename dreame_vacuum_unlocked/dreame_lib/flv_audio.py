"""
FLV/AAC muxer for the Dreame vacuum intercom audio uplink.

Reverse-engineered from the Dreame Android app (com.dreame.smartlife) via
Frida instrumentation of AudioRecordUtil / PCMEncoder / FLVPacker in
com.tencent.iot.video.link + com.tencent.iot.thirdparty.flv, and confirmed
byte-for-byte against a live capture (see ../captures/audio_uplink/).

Confirmed pipeline: mic PCM (16 kHz, mono, 16-bit) -> AAC-LC via MediaCodec
(96 kbps) -> standard FLV container (audio-only) -> raw bytes handed
straight to XP2P.dataSend() over the p2p "send service" channel armed by
XP2P.runSendService(channelId, "channel=<n>", crypto).

The FLV muxer itself (libmedia-server.so in the app) is the open-source
ireader/media-server library - nothing proprietary. This module reproduces
its output exactly: same FLV file header, same AudioSpecificConfig
derivation, same per-frame audio tag framing.

Captured ground truth (see captures/audio_uplink/000{1,3,5}_onFLV.bin):
    file header : 46 4c 56 01 04 00 00 00 09 00 00 00 00
    seq header  : 08 00 00 04 <ts:3><tsext:1> 00 00 00 af 00 14 08 00 00 00 0f
    audio frame : 08 00 03 02 <ts:3><tsext:1> 00 00 00 af 01 <768 bytes raw AAC> <prevsize:4>
"""

from __future__ import annotations

import struct
import subprocess
from dataclasses import dataclass
from typing import Iterator

FLV_TAG_AUDIO = 0x08

# AAC sampling-frequency-index table (ISO 14496-3), matches PCMEncoder.java's
# samplingFrequencyIndexMap in the decompiled app.
_SAMPLE_RATE_INDEX = {
    96000: 0, 88200: 1, 64000: 2, 48000: 3, 44100: 4, 32000: 5,
    24000: 6, 22050: 7, 16000: 8, 12000: 9, 11025: 10, 8000: 11,
}

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_AAC_BITRATE = 96000


def _prev_tag_size(tag: bytes) -> bytes:
    return struct.pack(">I", len(tag))


def _flv_tag(tag_type: int, payload: bytes, timestamp_ms: int) -> bytes:
    ts = timestamp_ms & 0xFFFFFFFF
    ts_lower24 = struct.pack(">I", ts & 0xFFFFFF)[1:]
    ts_ext = bytes([(ts >> 24) & 0xFF])
    header = bytes([tag_type]) + struct.pack(">I", len(payload))[1:] + ts_lower24 + ts_ext + b"\x00\x00\x00"
    tag = header + payload
    return tag + _prev_tag_size(tag)


def flv_file_header(has_audio: bool = True, has_video: bool = False) -> bytes:
    flags = (0x04 if has_audio else 0x00) | (0x01 if has_video else 0x00)
    header = b"FLV" + bytes([0x01, flags]) + struct.pack(">I", 9)
    return header + b"\x00\x00\x00\x00"  # PreviousTagSize0


def audio_specific_config(sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS,
                            audio_object_type: int = 2) -> bytes:
    """Build a 2-byte AAC AudioSpecificConfig (no SBR/PS extension). object_type=2 is AAC-LC."""
    freq_idx = _SAMPLE_RATE_INDEX[sample_rate]
    bits = (audio_object_type << 11) | (freq_idx << 7) | (channels << 3)  # + frameLen/depends/ext = 0
    return struct.pack(">H", bits)


def audio_sequence_header_tag(sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS,
                                timestamp_ms: int = 0) -> bytes:
    asc = audio_specific_config(sample_rate, channels)
    payload = bytes([0xAF, 0x00]) + asc
    return _flv_tag(FLV_TAG_AUDIO, payload, timestamp_ms)


def audio_raw_tag(aac_raw_frame: bytes, timestamp_ms: int) -> bytes:
    """aac_raw_frame must NOT include an ADTS header (raw AAC-LC ES only)."""
    payload = bytes([0xAF, 0x01]) + aac_raw_frame
    return _flv_tag(FLV_TAG_AUDIO, payload, timestamp_ms)


@dataclass
class AdtsFrame:
    raw_aac: bytes           # payload with the ADTS header stripped
    sample_rate: int
    channels: int


def iter_adts_frames(data: bytes) -> Iterator[AdtsFrame]:
    """Split a concatenated stream of ADTS-framed AAC (protection_absent=1, 7-byte header,
    which is what ffmpeg's `-f adts` output and PCMEncoder.java's addADTStoPacket both produce)
    into individual raw AAC frames."""
    i = 0
    n = len(data)
    while i + 7 <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xF0) != 0xF0:
            raise ValueError(f"bad ADTS sync at offset {i}")
        protection_absent = data[i + 1] & 0x01
        header_len = 7 if protection_absent else 9
        sample_rate_idx = (data[i + 2] >> 2) & 0x0F
        channels = ((data[i + 2] & 0x01) << 2) | ((data[i + 3] >> 6) & 0x03)
        frame_len = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | ((data[i + 5] >> 5) & 0x07)
        if frame_len < header_len or i + frame_len > n:
            raise ValueError(f"bad ADTS frame_len={frame_len} at offset {i}")
        sample_rate = next(sr for sr, idx in _SAMPLE_RATE_INDEX.items() if idx == sample_rate_idx)
        yield AdtsFrame(
            raw_aac=data[i + header_len:i + frame_len],
            sample_rate=sample_rate,
            channels=channels,
        )
        i += frame_len


def encode_pcm_to_adts(pcm_s16le: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE,
                        channels: int = DEFAULT_CHANNELS, bitrate: int = DEFAULT_AAC_BITRATE) -> bytes:
    """Shell out to ffmpeg to encode raw 16-bit PCM to AAC-LC in an ADTS stream,
    matching the app's MediaCodec settings (audio/mp4a-latm, aac-profile=LC, 96 kbps)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels), "-i", "pipe:0",
        "-c:a", "aac", "-b:a", str(bitrate), "-profile:a", "aac_low",
        "-f", "adts", "pipe:1",
    ]
    proc = subprocess.run(cmd, input=pcm_s16le, stdout=subprocess.PIPE, check=True)
    return proc.stdout


def mux_pcm_to_flv_packets(pcm_s16le: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE,
                             channels: int = DEFAULT_CHANNELS,
                             bitrate: int = DEFAULT_AAC_BITRATE) -> list[bytes]:
    """Full pipeline: raw PCM bytes -> list of FLV packets ready to hand to
    XP2P.dataSend()/QcloudSendVoice() one at a time, in order.

    packets[0] is the FLV file header, packets[1] is the audio sequence header
    (AudioSpecificConfig), and packets[2:] are one raw-AAC audio tag per encoded
    frame (~64ms of audio each at 16 kHz). This exactly mirrors the sequence of
    dataSend() calls observed from the real app.
    """
    adts = encode_pcm_to_adts(pcm_s16le, sample_rate, channels, bitrate)
    packets = [flv_file_header(has_audio=True, has_video=False)]
    packets.append(audio_sequence_header_tag(sample_rate, channels, timestamp_ms=0))

    frame_duration_ms = 1024 * 1000 // sample_rate  # AAC-LC = 1024 samples/frame
    ts = 0
    for frame in iter_adts_frames(adts):
        packets.append(audio_raw_tag(frame.raw_aac, timestamp_ms=ts))
        ts += frame_duration_ms
    return packets


def wav_pcm_bytes(path: str, sample_rate: int = DEFAULT_SAMPLE_RATE,
                  channels: int = DEFAULT_CHANNELS) -> bytes | None:
    """Pull the PCM payload out of a WAV file written as s16le @16k mono,
    without spawning ffmpeg.

    The add-on pre-converts every uploaded mp3 to a sibling `<name>.wav` (see
    app.ensure_audio_wav) so play-time skips the slow mp3 decode. If `path` is
    such a WAV (PCM, 16-bit, matching sample rate/channels) this returns the
    raw PCM bytes directly; otherwise None and the caller falls back to an
    ffmpeg decode. Conservative on purpose: any mismatch (compressed wav,
    different rate/channels, malformed header) bails to ffmpeg rather than
    risking garbage on the speaker.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    i = 12
    audio_format = channels_w = rate = bits = None
    data_chunk = None
    while i + 8 <= len(data):
        cid = data[i:i + 4]
        size = int.from_bytes(data[i + 4:i + 8], "little")
        body = data[i + 8:i + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            audio_format = int.from_bytes(body[0:2], "little")
            channels_w = int.from_bytes(body[2:4], "little")
            rate = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little")
        elif cid == b"data":
            data_chunk = body
        i += 8 + size + (size & 1)  # chunks are word-aligned
    if audio_format not in (1, 0xFFFE):
        return None  # 0xFFFE = WAVE_FORMAT_EXTENSIBLE (still PCM underneath)
    if bits == 16 and rate == sample_rate and channels_w == channels and data_chunk is not None:
        return data_chunk
    return None


def decode_any_to_pcm(input_path: str, sample_rate: int = DEFAULT_SAMPLE_RATE,
                       channels: int = DEFAULT_CHANNELS) -> bytes:
    """Normalize any input audio file (wav/mp3/m4a/whatever) to raw 16-bit
    little-endian PCM at the given sample rate/channels.

    A pre-converted `<name>.wav` sibling that already matches the target is
    read directly (no subprocess); everything else goes through ffmpeg.
    """
    pcm = wav_pcm_bytes(input_path, sample_rate, channels)
    if pcm is not None:
        return pcm
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-ar", str(sample_rate), "-ac", str(channels),
        "-f", "s16le", "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, check=True)
    return proc.stdout


def build_send_file(input_audio_path: str, output_flv_path: str,
                     sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS,
                     bitrate: int = DEFAULT_AAC_BITRATE) -> int:
    """End-to-end: any input audio file -> a single concatenated FLV/AAC file
    ready to hand to pc_client's SEND_AUDIO_FILE env var (which walks it tag
    by tag and feeds each chunk to QcloudSendVoice). Returns packet count."""
    pcm = decode_any_to_pcm(input_audio_path, sample_rate, channels)
    packets = mux_pcm_to_flv_packets(pcm, sample_rate, channels, bitrate)
    with open(output_flv_path, "wb") as f:
        for p in packets:
            f.write(p)
    return len(packets)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Build a pre-muxed FLV/AAC file for the Dreame intercom uplink "
                    "(feed the output to pc_client via SEND_AUDIO_FILE=<path>).")
    parser.add_argument("input_audio", help="any ffmpeg-readable audio file (wav, mp3, m4a, ...)")
    parser.add_argument("output_flv", help="output path, e.g. uplink.flv")
    args = parser.parse_args()

    n = build_send_file(args.input_audio, args.output_flv)
    print(f"wrote {n} packets to {args.output_flv}")
