"""Camera diagnostic. Run this before the kiosk, not after it fails.

    python scripts/check_camera.py --list        # what cameras exist, and their formats
    python scripts/check_camera.py               # open one and decode for 15 seconds
    python scripts/check_camera.py --camera web  # pick by name substring

The point of a separate script is separating three failures that look identical from
inside the kiosk: the camera not opening, the code not decoding, and the code decoding
but being rejected by the HMAC check. This tells you which one you have.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_API", "pyside6")

import dataclasses

from trackify.core import qrcodes
from trackify.core.config import load_config


def show_devices() -> int:
    from trackify.ui.camera import MULTIMEDIA, list_cameras

    if not MULTIMEDIA:
        print("QtMultimedia is unavailable. On the Pi:")
        print("  sudo apt install gstreamer1.0-plugins-base gstreamer1.0-plugins-good "
              "gstreamer1.0-libav")
        return 1

    devices = list_cameras()
    if not devices:
        print("No cameras found.")
        print("On the Pi, use a USB UVC webcam -- the CSI Camera Module does not")
        print("present itself through V4L2 on Bookworm or Trixie.")
        return 1

    for device in devices:
        print(f"\n  {device.description()}")
        seen = set()
        for fmt in device.videoFormats():
            size = fmt.resolution()
            key = (size.width(), size.height(), str(fmt.pixelFormat()).split(".")[-1])
            if key in seen:
                continue
            seen.add(key)
            print(f"      {key[0]:>5} x {key[1]:<5} {fmt.maxFrameRate():>5.0f} fps  "
                  f"{key[2]}")
    print("\nPass a substring of a name to --camera, or set device in [camera].")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TRACKIFY camera diagnostic")
    parser.add_argument("--list", action="store_true", help="list cameras and exit")
    parser.add_argument("--camera", default=None, metavar="NAME")
    parser.add_argument("--seconds", type=int, default=15)
    args = parser.parse_args(argv)

    from qtpy.QtCore import QTimer
    from qtpy.QtWidgets import QApplication, QVBoxLayout, QWidget

    app = QApplication(sys.argv)          # QMediaDevices needs one, even to enumerate

    if args.list:
        return show_devices()

    config = load_config()
    camera_config = dataclasses.replace(
        config.camera,
        enabled=True,
        device=args.camera if args.camera is not None else config.camera.device,
    )

    from trackify.ui.camera import CameraPanel

    window = QWidget()
    window.setWindowTitle("TRACKIFY camera check")
    window.resize(760, 520)
    layout = QVBoxLayout(window)
    panel = CameraPanel(camera_config)
    layout.addWidget(panel)

    seen: dict[str, int] = {}

    def on_code(payload: str) -> None:
        seen[payload] = seen.get(payload, 0) + 1
        # Decoding a code and accepting it are different things. Say which happened.
        try:
            student_id = qrcodes.decode(payload, config.secrets.qr_secret)
            verdict = f"valid, student {student_id}"
        except qrcodes.InvalidQRCode as exc:
            verdict = f"REJECTED: {exc}"
        except ValueError as exc:
            verdict = f"cannot verify: {exc}"
        print(f"  read  {payload:<24} {verdict}")

    def on_status(state: str, message: str) -> None:
        print(f"  camera: {state} -- {message}")

    panel.code_detected.connect(on_code)
    panel.status_changed.connect(on_status)

    print(f"\nHold a QR code up to the lens. Watching for {args.seconds}s.\n")
    window.show()
    panel.start()

    def finish() -> None:
        worker = panel._worker
        print("\n  frames decoded : "
              f"{worker.decoded if worker else 0}")
        if worker and worker.decoded:
            print(f"  last decode    : {worker.last_ms:.1f} ms")
        print(f"  distinct codes : {len(seen)}")
        for payload, count in seen.items():
            # More than one fire per presentation means the gate let a repeat through.
            print(f"      {payload}  fired {count}x")
        if not seen:
            print("\n  Nothing decoded. In order of likelihood:")
            print("    - the printed code is too small (aim for 25 mm or wider)")
            print("    - glare on lamination (tilt the card, or the camera)")
            print("    - the card is closer than the minimum focus distance")
        panel.shutdown()
        app.quit()

    QTimer.singleShot(args.seconds * 1000, finish)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
