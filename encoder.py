"""
QR Video Generator (POC)
------------------------
Object-oriented Python implementation to turn a file into a stream of
QR-code frames suitable for HDMI->capture transfers.

Features (POC level):
- Versioned frame header with CRCs
- Manifest / Data / EOF frames
- Optional, minimal FEC placeholder (copies of data) to show structure
- Renders centered QR on a neutral background for clean capture
- Saves PNG frame sequence; optional MP4 if imageio-ffmpeg is present

Dependencies:
  pip install pillow qrcode imageio imageio-ffmpeg
  (imageio-ffmpeg is optional; otherwise you'll get PNG frames only)

Usage:
  python qr_video_poc.py --in sample.bin --out ./out_frames --fps 30 \
      --ecc L --chunk 1800 --qr-version auto --video 1920x1080

This is a proof-of-concept; swap SimpleFEC for a proper RS/RaptorQ for production.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import random
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw
except Exception as e:  # pragma: no cover
    raise SystemExit("Pillow is required: pip install pillow")

try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H
except Exception as e:  # pragma: no cover
    raise SystemExit("qrcode is required: pip install qrcode[pil]")

# imageio is optional; used for mp4 write
try:  # pragma: no cover
    import imageio.v3 as iio
    _HAS_IMAGEIO = True
except Exception:
    _HAS_IMAGEIO = False


# =============================
# Utilities
# =============================

def crc32(data: bytes) -> int:
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


def crc16_ccitt(data: bytes, poly: int = 0x1021, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE implementation."""
    crc = init
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            if (crc & 0x8000) != 0:
                crc = (crc << 1) ^ poly
            else:
                crc = (crc << 1)
            crc &= 0xFFFF
    return crc


def chunks(b: bytes, size: int) -> Iterable[bytes]:
    for i in range(0, len(b), size):
        yield b[i : i + size]


# =============================
# Protocol Data Structures
# =============================

class FrameType:
    SYNC = 0
    MANIFEST = 1
    DATA = 2
    PARITY = 3
    HEARTBEAT = 4
    EOF = 5


@dataclasses.dataclass
class QRV1Header:
    frame_type: int
    version: int
    file_id: bytes
    seq: int
    block_id: int
    index_in_block: int
    total_blocks: int
    payload_len: int

    MAGIC = b"QRV1"

    def pack(self) -> bytes:
        # Header without CRC16
        hdr = (
            self.MAGIC
            + struct.pack(
                "<BB",
                self.frame_type & 0xFF,
                self.version & 0xFF,
            )
            + self.file_id
            + struct.pack(
                "<I I H I H",
                self.seq,
                self.block_id,
                self.index_in_block,
                self.total_blocks,
                self.payload_len,
            )
        )
        hcrc = crc16_ccitt(hdr)
        return hdr + struct.pack("<H", hcrc)

    @staticmethod
    def unpack(buf: bytes) -> Tuple["QRV1Header", int]:
        if len(buf) < 4 + 1 + 1 + 16 + 4 + 4 + 2 + 4 + 2 + 2:
            raise ValueError("header too short")
        off = 0
        if buf[:4] != QRV1Header.MAGIC:
            raise ValueError("bad magic")
        off += 4
        frame_type, ver = struct.unpack_from("<BB", buf, off)
        off += 2
        file_id = buf[off : off + 16]
        off += 16
        seq, block_id, index_in_block, total_blocks, payload_len = struct.unpack_from(
            "<I I H I H", buf, off
        )
        off += struct.calcsize("<I I H I H")
        hcrc, = struct.unpack_from("<H", buf, off)
        off += 2
        hdr_wo_crc = buf[: off - 2]
        if crc16_ccitt(hdr_wo_crc) != hcrc:
            raise ValueError("header CRC mismatch")
        return (
            QRV1Header(
                frame_type=frame_type,
                version=ver,
                file_id=file_id,
                seq=seq,
                block_id=block_id,
                index_in_block=index_in_block,
                total_blocks=total_blocks,
                payload_len=payload_len,
            ),
            off,
        )


class FrameBuilder:
    """Builds binary payloads for QR frames from headers + payload."""

    def wrap(self, header: QRV1Header, payload: bytes) -> bytes:
        hdr = header.pack()
        pcrc = crc32(payload)
        return hdr + payload + struct.pack("<I", pcrc)


class QRRenderer:
    """Renders QR payloads into PIL Images, centered on a background.

    For video-capture robustness, we render a large QR with a quiet zone and
    neutral gray background to minimize ringing.
    """

    ECC_MAP = {
        "L": ERROR_CORRECT_L,
        "M": ERROR_CORRECT_M,
        "Q": ERROR_CORRECT_Q,
        "H": ERROR_CORRECT_H,
    }

    def __init__(
        self,
        frame_wh: Tuple[int, int] = (1920, 1080),
        ecc: str = "M",
        qr_version: Optional[int] = None,
        quiet_zone_modules: int = 4,
        fill_background: Tuple[int, int, int] = (200, 200, 200),
        fill_qr: Tuple[int, int, int] = (0, 0, 0),
        fill_bg_qr: Tuple[int, int, int] = (255, 255, 255),
        qr_scale_hint: float = 0.9,
    ):
        self.frame_w, self.frame_h = frame_wh
        self.ecc = ecc.upper()
        self.qr_version = qr_version  # None => auto
        self.quiet_zone_modules = quiet_zone_modules
        self.bg = fill_background
        self.qr_fg = fill_qr
        self.qr_bg = fill_bg_qr
        self.scale_hint = qr_scale_hint

    def _make_qr_image(self, payload: bytes) -> Image.Image:
        qr = qrcode.QRCode(
            version=self.qr_version if self.qr_version not in (None, "auto") else None,
            error_correction=self.ECC_MAP[self.ecc],
            box_size=10,  # we will rescale later
            border=self.quiet_zone_modules,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color=self.qr_fg, back_color=self.qr_bg).convert("RGB")
        return img

    def render_frame(self, payload: bytes) -> Image.Image:
        base = Image.new("RGB", (self.frame_w, self.frame_h), self.bg)
        qr_img = self._make_qr_image(payload)
        # Compute max square area we allow for the QR image
        side = int(min(self.frame_w, self.frame_h) * self.scale_hint)
        qr_img = qr_img.resize((side, side), resample=Image.NEAREST)
        x = (self.frame_w - side) // 2
        y = (self.frame_h - side) // 2
        base.paste(qr_img, (x, y))
        return base


class SimpleFEC:
    """A placeholder FEC that simply repeats the first K frames as 'parity'.
    Replace with real RS/RaptorQ for production.
    """

    def __init__(self, n_data: int, k_parity: int):
        self.n = n_data
        self.k = k_parity

    def encode_block(self, block_payloads: List[bytes]) -> List[bytes]:
        if len(block_payloads) != self.n:
            # pad to n with empty bytes for shape
            block_payloads = block_payloads + [b"" for _ in range(self.n - len(block_payloads))]
        # Return k parity payloads (here: copies of the first k data payloads)
        parity = [block_payloads[i % self.n] for i in range(self.k)]
        return parity


@dataclasses.dataclass
class GeneratorConfig:
    chunk_size: int = 1800
    n_data: int = 60
    k_parity: int = 12
    fps: int = 30
    ecc: str = "L"
    qr_version: Optional[int] = None  # None/"auto" for auto version
    frame_size: Tuple[int, int] = (1920, 1080)
    repeat_manifest: int = 60
    repeat_eof: int = 90
    interleave_blocks: bool = False  # set True for burst loss resilience


class QRVideoGenerator:
    def __init__(self, cfg: GeneratorConfig):
        self.cfg = cfg
        self.file_id = uuid.uuid4().bytes
        self.fb = FrameBuilder()
        self.renderer = QRRenderer(
            frame_wh=cfg.frame_size,
            ecc=cfg.ecc,
            qr_version=cfg.qr_version,
            qr_scale_hint=0.9,
        )
        self.fec = SimpleFEC(cfg.n_data, cfg.k_parity)
        self.seq = 0

    # ---------- Protocol payload builders ----------
    def _manifest_bytes(self, file_name: str, file_size: int, total_blocks: int) -> bytes:
        manifest = {
            "file_name": file_name,
            "file_size": file_size,
            "file_sha256": None,  # compute offline if desired
            "chunk_size": self.cfg.chunk_size,
            "fec": {"type": "SIMPLE", "n": self.cfg.n_data, "k": self.cfg.k_parity},
            "fps": self.cfg.fps,
            "qr": {"version": self.cfg.qr_version or "auto", "ecc": self.cfg.ecc},
            "video": {"width": self.cfg.frame_size[0], "height": self.cfg.frame_size[1]},
            "repeat_manifest": self.cfg.repeat_manifest,
        }
        return json.dumps(manifest, separators=(",", ":")).encode("utf-8")

    def _wrap(self, frame_type: int, block_id: int, idx_in_block: int, total_blocks: int, payload: bytes) -> bytes:
        header = QRV1Header(
            frame_type=frame_type,
            version=1,
            file_id=self.file_id,
            seq=self.seq,
            block_id=block_id,
            index_in_block=idx_in_block,
            total_blocks=total_blocks,
            payload_len=len(payload),
        )
        self.seq += 1
        return self.fb.wrap(header, payload)

    # ---------- Frame generation ----------
    def generate_frames(self, file_path: Path) -> Iterable[Image.Image]:
        data = file_path.read_bytes()
        file_name = file_path.name
        total_chunks = math.ceil(len(data) / self.cfg.chunk_size)
        total_blocks = math.ceil(total_chunks / self.cfg.n_data)

        # Emit MANIFEST repeatedly
        manifest_payload = self._manifest_bytes(file_name, len(data), total_blocks)
        for _ in range(self.cfg.repeat_manifest):
            pkt = self._wrap(FrameType.MANIFEST, 0, 0, total_blocks, manifest_payload)
            yield self.renderer.render_frame(pkt)

        # Prepare chunks
        chunk_list = list(chunks(data, self.cfg.chunk_size))

        if not self.cfg.interleave_blocks:
            # Emit block by block
            for b in range(total_blocks):
                start = b * self.cfg.n_data
                end = min((b + 1) * self.cfg.n_data, total_chunks)
                block_data = chunk_list[start:end]
                # pad to n for parity shaping
                if len(block_data) < self.cfg.n_data:
                    block_data = block_data + [b"" for _ in range(self.cfg.n_data - len(block_data))]

                # DATA frames
                for i, pl in enumerate(block_data):
                    pkt = self._wrap(FrameType.DATA, b, i, total_blocks, pl)
                    yield self.renderer.render_frame(pkt)

                # PARITY frames (placeholder FEC)
                for i, ppl in enumerate(self.fec.encode_block(block_data)):
                    pkt = self._wrap(FrameType.PARITY, b, self.cfg.n_data + i, total_blocks, ppl)
                    yield self.renderer.render_frame(pkt)
        else:
            # Simple interleaving across blocks by index
            # Build per-block lists first
            per_block = []
            for b in range(total_blocks):
                start = b * self.cfg.n_data
                end = min((b + 1) * self.cfg.n_data, total_chunks)
                block_data = chunk_list[start:end]
                if len(block_data) < self.cfg.n_data:
                    block_data = block_data + [b"" for _ in range(self.cfg.n_data - len(block_data))]
                per_block.append((b, block_data, self.fec.encode_block(block_data)))

            # Interleave DATA by index
            for i in range(self.cfg.n_data):
                for b, block_data, _ in per_block:
                    pkt = self._wrap(FrameType.DATA, b, i, total_blocks, block_data[i])
                    yield self.renderer.render_frame(pkt)
            # Interleave PARITY by index
            for i in range(self.cfg.k_parity):
                for b, _, parities in per_block:
                    pkt = self._wrap(FrameType.PARITY, b, self.cfg.n_data + i, total_blocks, parities[i])
                    yield self.renderer.render_frame(pkt)

        # EOF frames
        eof_payload = b"EOF"
        for _ in range(self.cfg.repeat_eof):
            pkt = self._wrap(FrameType.EOF, total_blocks, 0, total_blocks, eof_payload)
            yield self.renderer.render_frame(pkt)

    # ---------- Output helpers ----------
    def save_png_sequence(self, frames: Iterable[Image.Image], out_dir: Path) -> List[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: List[Path] = []
        for idx, img in enumerate(frames):
            p = out_dir / f"frame_{idx:06d}.png"
            img.save(p)
            paths.append(p)
        return paths

    def save_mp4(self, frames: Iterable[Image.Image], out_path: Path, fps: int) -> None:
        if not _HAS_IMAGEIO:
            raise RuntimeError("imageio not available; install imageio and imageio-ffmpeg")
        # Collect frames; for very large transfers stream-writing would be better
        arr = [img for img in frames]
        # imageio.v3.imwrite handles ffmpeg if plugin is available
        iio.imwrite(out_path.as_posix(), arr, fps=fps)


# =============================
# CLI
# =============================

def parse_size_wh(s: str) -> Tuple[int, int]:
    try:
        w, h = s.lower().split("x")
        return int(w), int(h)
    except Exception:
        raise argparse.ArgumentTypeError("--video must be like 1920x1080")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="QR Video Generator (POC)")
    ap.add_argument("--in", dest="infile", required=True, help="Input file to transmit")
    ap.add_argument("--out", dest="out", required=True, help="Output directory or mp4 path")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--ecc", choices=["L", "M", "Q", "H"], default="L")
    ap.add_argument("--chunk", type=int, default=1800, help="Payload bytes per frame")
    ap.add_argument("--n", type=int, default=60, help="Data frames per block")
    ap.add_argument("--k", type=int, default=12, help="Parity frames per block (placeholder)")
    ap.add_argument("--qr-version", default="auto", help="QR version 1..40 or 'auto'")
    ap.add_argument("--video", default="1920x1080", type=parse_size_wh, help="Frame size, e.g., 1920x1080")
    ap.add_argument("--manifest-repeats", type=int, default=60)
    ap.add_argument("--eof-repeats", type=int, default=90)
    ap.add_argument("--interleave", action="store_true", help="Interleave blocks by index for burst-loss tolerance")
    ap.add_argument("--mp4", action="store_true", help="Write an MP4 file instead of PNG sequence (requires imageio-ffmpeg)")
    args = ap.parse_args(argv)

    qr_version = None if args.qr_version in ("auto", None) else int(args.qr_version)

    cfg = GeneratorConfig(
        chunk_size=args.chunk,
        n_data=args.n,
        k_parity=args.k,
        fps=args.fps,
        ecc=args.ecc,
        qr_version=qr_version,
        frame_size=args.video,
        repeat_manifest=args.manifest_repeats,
        repeat_eof=args.eof_repeats,
        interleave_blocks=args.interleave,
    )

    gen = QRVideoGenerator(cfg)

    infile = Path(args.infile)
    if not infile.exists():
        print(f"Input file not found: {infile}", file=sys.stderr)
        return 2

    frames_iter = gen.generate_frames(infile)

    out = Path(args.out)

    if args.mp4:
        if out.suffix.lower() != ".mp4":
            out = out.with_suffix(".mp4")
        print(f"Writing MP4 to {out} at {cfg.fps} fps ...")
        gen.save_mp4(frames_iter, out, cfg.fps)
        print("Done.")
    else:
        print(f"Writing PNG frames to {out} ...")
        paths = gen.save_png_sequence(frames_iter, out)
        print(f"Wrote {len(paths)} frames to {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
