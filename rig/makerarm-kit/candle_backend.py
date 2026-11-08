"""candleLight (gs_usb) USB-to-CAN adapters for the Maker Arm SDK.

candleLight adapters (USB ID 1d50:606f) don't show up as a COM port. On Linux they are a
SocketCAN interface (can0) that the SDK already supports; on Windows/macOS this backend
talks to them directly over USB through python-can's gs_usb interface.

Port syntax used by the rest of the kit:  candle   (first adapter)   candle:1   (second one)
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

from maker_arm.transport.base import CanBackend, CanId

log = logging.getLogger("maker_arm.transport")
BITRATE = 1_000_000  # the Maker Arm bus runs at 1 Mbps


def _expose_libusb() -> None:
    """Make the libusb DLL bundled by `libusb-package` findable by pyusb (Windows/macOS)."""
    try:
        import libusb_package
    except ImportError:
        return
    path = libusb_package.get_library_path()
    if path:
        folder = os.path.dirname(str(path))
        if folder not in os.environ.get("PATH", ""):
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(folder)


def release_usb(dev=None) -> None:
    """Really let go of the adapter. python-can's gs_usb shutdown() stops the CAN channel but keeps the
    pyusb handle open (and re-scans, opening more); with WinUSB only one handle may exist, so the next
    open in this process fails with "[Errno 13] Access denied" until those objects are collected."""
    import gc
    if dev is not None:
        try:
            import usb.util
            usb.util.dispose_resources(dev)
        except Exception:  # noqa: BLE001
            pass
    gc.collect()


def is_candle_port(port: str | None) -> bool:
    return bool(port) and port.lower().split(":")[0] == "candle"


def candle_index(port: str) -> int:
    _, _, idx = port.partition(":")
    return int(idx or 0)


def list_candle_devices() -> list[str]:
    """Return 'candle:N' for each candleLight adapter found (empty if pyusb/libusb missing)."""
    try:
        _expose_libusb()
        import usb.core
        devs = list(usb.core.find(find_all=True, idVendor=0x1D50, idProduct=0x606F))
    except Exception as exc:  # noqa: BLE001
        log.debug("candleLight scan failed: %s", exc)
        return []
    return [f"candle:{i}" for i in range(len(devs))]


class CandleBackend(CanBackend):
    def __init__(self, index: int = 0, bitrate: int = BITRATE):
        self._index, self._bitrate = index, bitrate
        self._bus = None
        self._cb: Optional[Callable[[int, bytes], None]] = None
        self._tx_lock = threading.Lock()
        self._running = False
        self._rx: Optional[threading.Thread] = None
        self.dropped = 0
        self._last_drop_log = 0.0

    def open(self) -> None:
        _expose_libusb()
        import can
        self._bus = can.Bus(interface="gs_usb", channel=self._index, index=self._index, bitrate=self._bitrate)
        self._running = True
        self._rx = threading.Thread(target=self._rx_loop, daemon=True, name=f"candle-rx-{self._index}")
        self._rx.start()

    def close(self) -> None:
        self._running = False
        if self._rx:
            self._rx.join(timeout=1.0)
            self._rx = None
        if self._bus:
            dev = getattr(getattr(self._bus, "gs_usb", None), "gs_usb", None)  # the pyusb device
            try:
                self._bus.shutdown()
            finally:
                self._bus = None
                release_usb(dev)

    def send(self, can_id: int, data: bytes, extended: bool = True) -> None:
        import can
        msg = can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=extended)
        with self._tx_lock:
            bus = self._bus
            if bus is None:
                self._dropped("adapter closed")
                return
            try:
                bus.send(msg, timeout=0.05)
            except (can.CanError, OSError) as exc:
                # A transient USB/CAN hiccup must not take the whole arm down: log and drop this
                # frame. The SDK still faults safely (holds) if feedback stays missing > 250 ms.
                self._dropped(str(exc))

    def _dropped(self, reason: str) -> None:
        import time
        self.dropped += 1
        now = time.monotonic()
        if now - self._last_drop_log > 2.0:  # at most one log line every 2 s
            log.warning("candleLight %d: dropped %d frame(s), last: %s", self._index, self.dropped, reason)
            self._last_drop_log = now

    def set_recv_callback(self, cb) -> None:
        self._cb = cb

    def _rx_loop(self) -> None:
        import time
        while self._running:
            bus = self._bus
            if bus is None:
                break
            try:
                msg = bus.recv(timeout=0.1)
            except Exception as exc:  # noqa: BLE001
                if self._running:
                    log.warning("candleLight receive failed (%s); retrying", exc)
                time.sleep(0.2)
                continue
            if msg is None or msg.is_error_frame or msg.is_remote_frame:
                continue
            cb = self._cb
            if cb:
                try:
                    cb(CanId(msg.arbitration_id, msg.is_extended_id), bytes(msg.data))
                except Exception:  # noqa: BLE001
                    log.exception("candleLight RX frame-processing exception (continuing)")
