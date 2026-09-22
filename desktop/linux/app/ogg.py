from __future__ import annotations

import struct


def ogg_crc(data: bytes) -> int:
    crc = 0
    for value in data:
        crc ^= value << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


class OggOpusMuxer:
    def __init__(self, sample_rate: int = 16_000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.serial = 0x5653544B
        self.sequence = 0
        self.granule = 0
        self.wrote_headers = False

    def reset(self) -> bytes:
        self.serial = 0x5653544B
        self.sequence = 0
        self.granule = 0
        self.wrote_headers = False
        return b""

    def _headers(self) -> bytes:
        head = (
            b"OpusHead"
            + bytes((1, self.channels))
            + struct.pack("<HIhB", 312, self.sample_rate, 0, 0)
        )
        tags = b"OpusTags" + struct.pack("<I", 8) + b"XC Buddy" + struct.pack("<I", 0)
        self.wrote_headers = True
        return self._page(head, header_type=2, granule=0) + self._page(
            tags, header_type=0, granule=0
        )

    def packet(self, packet: bytes, end: bool = False) -> bytes:
        if not packet:
            raise ValueError("empty Opus payloads must be written with finish()")
        if len(packet) > 255:
            raise ValueError("v1 muxer expects one lacing segment per packet")
        output = b"" if self.wrote_headers else self._headers()
        self.granule += 960 * 48_000 // self.sample_rate
        return output + self._page(
            packet, header_type=4 if end else 0, granule=self.granule
        )

    def finish(self) -> bytes:
        output = b"" if self.wrote_headers else self._headers()
        header = bytearray(b"OggS\x00\x04")
        header += struct.pack("<QIIIB", self.granule, self.serial, self.sequence, 0, 0)
        struct.pack_into("<I", header, 22, ogg_crc(header))
        self.sequence += 1
        return output + bytes(header)

    def _page(self, payload: bytes, header_type: int, granule: int) -> bytes:
        header = bytearray(b"OggS\x00" + bytes((header_type,)))
        header += struct.pack(
            "<QIIIBB", granule, self.serial, self.sequence, 0, 1, len(payload)
        )
        page = header + payload
        struct.pack_into("<I", page, 22, ogg_crc(page))
        self.sequence += 1
        return bytes(page)
