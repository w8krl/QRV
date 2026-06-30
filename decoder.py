"""
QR Video Decoder (POC)
----------------------
Decodes a PNG frame sequence produced by encoder.py back into the original file.

Dependencies:
  pip install zxing-cpp opencv-python-headless

Usage:
  python decoder.py --in ./qr_frames --out decoded.bin
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import cv2
except ImportError:
    raise SystemExit("opencv-python-headless is required: pip install opencv-python-headless")

try:
    from zxingcpp import read_barcode
except ImportError:
    raise SystemExit("zxing-cpp is required: pip install zxing-cpp")


# =============================
# Utilities (must match encoder)
# =============================

def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def crc16_ccitt(data: bytes, poly: int = 0x1021, init: int = 0xFFFF) -> int:
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


# =============================
# Protocol (QRV1 — matches encoder)
# =============================

class FrameType:
    SYNC = 0
    MANIFEST = 1
    DATA = 2
    PARITY = 3
    HEARTBEAT = 4
    EOF = 5


def parse_qrv1_header(buf: bytes) -> Tuple[dict, int]:
    magic = b"QRV1"
    min_len = 4 + 2 + 16 + struct.calcsize("<I I H I H") + 2
    if len(buf) < min_len:
        raise ValueError("header too short")
    if buf[:4] != magic:
        raise ValueError("bad magic")

    off = 4
    frame_type, version = struct.unpack_from("<BB", buf, off)
    off += 2
    file_id = buf[off : off + 16]
    off += 16
    seq, block_id, index_in_block, total_blocks, payload_len = struct.unpack_from(
        "<I I H I H", buf, off
    )
    off += struct.calcsize("<I I H I H")
    hdr_crc, = struct.unpack_from("<H", buf, off)
    off += 2

    if crc16_ccitt(buf[: off - 2]) != hdr_crc:
        raise ValueError("header CRC mismatch")

    return (
        {
            "frame_type": frame_type,
            "version": version,
            "file_id": file_id,
            "seq": seq,
            "block_id": block_id,
            "index_in_block": index_in_block,
            "total_blocks": total_blocks,
            "payload_len": payload_len,
        },
        off,
    )


def decode_qr_payload(data_bytes: bytes) -> Tuple[dict, bytes]:
    header, off = parse_qrv1_header(data_bytes)
    payload_len = header["payload_len"]
    payload = data_bytes[off : off + payload_len]
    if len(payload) != payload_len:
        raise ValueError("truncated payload")
    pcrc, = struct.unpack("<I", data_bytes[off + payload_len : off + payload_len + 4])
    if crc32(payload) != pcrc:
        raise ValueError("payload CRC mismatch")
    return header, payload


def decode_qr_frame(frame_path: Path) -> Tuple[dict, bytes]:
    img = cv2.imread(str(frame_path))
    if img is None:
        raise ValueError(f"could not read image: {frame_path}")

    result = read_barcode(img)
    if not result or not result.bytes:
        raise ValueError(f"no QR detected in {frame_path.name}")

    return decode_qr_payload(bytes(result.bytes))


# =============================
# Client
# =============================

class QRVideoClient:
    """Decode a folder of PNG frames and reconstruct the transmitted file."""

    def __init__(self, folder: Path, verbose: bool = False):
        self.folder = folder
        self.verbose = verbose
        self.frames = sorted(folder.glob("frame_*.png"))
        if not self.frames:
            raise FileNotFoundError(f"no frame_*.png files in {folder}")

        self.manifest: Optional[dict] = None
        self.total_blocks: Optional[int] = None
        self.data_blocks: Dict[int, Dict[int, bytes]] = {}
        self.frames_decoded = 0
        self.frames_failed = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def process_frames(self) -> None:
        for fp in self.frames:
            try:
                header, payload = decode_qr_frame(fp)
            except Exception as e:
                self.frames_failed += 1
                print(f"[WARN] {fp.name}: {e}", file=sys.stderr)
                continue

            self.frames_decoded += 1
            frame_type = header["frame_type"]

            if frame_type == FrameType.MANIFEST:
                try:
                    manifest = json.loads(payload.decode("utf-8"))
                    self.manifest = manifest
                    self.total_blocks = header["total_blocks"]
                    self._log(f"manifest: {manifest.get('file_name')} ({manifest.get('file_size')} bytes)")
                except Exception as e:
                    print(f"[WARN] bad manifest in {fp.name}: {e}", file=sys.stderr)

            elif frame_type == FrameType.DATA:
                block_id = header["block_id"]
                index = header["index_in_block"]
                self.data_blocks.setdefault(block_id, {})[index] = payload

            elif frame_type == FrameType.PARITY:
                pass  # placeholder FEC not used on decode yet

            elif frame_type == FrameType.EOF:
                self._log(f"EOF seen in {fp.name}")

        print(
            f"Processed {len(self.frames)} frames "
            f"({self.frames_decoded} decoded, {self.frames_failed} failed)."
        )
        if self.manifest:
            print(
                f"Manifest: {self.manifest.get('file_name')} — "
                f"{self.manifest.get('file_size')} bytes"
            )
        print(f"Collected {len(self.data_blocks)} data blocks.")

    def reconstruct(self, out_path: Path) -> Path:
        if not self.data_blocks:
            raise RuntimeError("no data blocks decoded")

        n_data = 60
        if self.manifest:
            fec = self.manifest.get("fec") or {}
            n_data = int(fec.get("n", n_data))

        data = bytearray()
        for block_id in sorted(self.data_blocks):
            idx_map = self.data_blocks[block_id]
            for index in sorted(idx_map):
                if index >= n_data:
                    continue
                chunk = idx_map[index]
                if not chunk:
                    continue
                data.extend(chunk)

        if self.manifest and "file_size" in self.manifest:
            expected = int(self.manifest["file_size"])
            if len(data) < expected:
                print(
                    f"[WARN] reconstructed {len(data)} bytes, expected {expected}",
                    file=sys.stderr,
                )
            data = data[:expected]

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        print(f"Reconstructed file written to {out_path} ({len(data)} bytes)")
        return out_path


# =============================
# CLI
# =============================

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="QR Video Decoder (POC)")
    ap.add_argument("--in", dest="indir", required=True, help="Input directory of PNG frames")
    ap.add_argument("--out", dest="outfile", required=True, help="Output reconstructed file path")
    ap.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = ap.parse_args(argv)

    indir = Path(args.indir)
    if not indir.is_dir():
        print(f"Input directory not found: {indir}", file=sys.stderr)
        return 2

    client = QRVideoClient(indir, verbose=args.verbose)
    client.process_frames()
    client.reconstruct(Path(args.outfile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
