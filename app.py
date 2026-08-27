"""TRACKIFY entry point.

    python app.py --windowed        # laptop development
    python app.py                   # kiosk: fullscreen, frameless

--windowed matters during development: a fullscreen always-on-top kiosk is hostile on
a machine you are still working on.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")

from qtpy.QtCore import QThread
from qtpy.QtWidgets import QApplication

from trackify.core import db
from trackify.core.config import load_config
from trackify.core.service import ScanService
from trackify.ui.kiosk import KioskWindow
from trackify.ui.worker import SmsWorker

STYLE = Path(__file__).parent / "trackify" / "ui" / "style.qss"


def build_provider(name: str, config):
    """Console and Null never spend load and never text a real parent."""
    if name == "gsm":
        from trackify.notify.gsm import GsmProvider
        return GsmProvider(
            config.gsm.port or None,
            baud=config.gsm.baud,
            send_timeout=config.gsm.send_timeout_s,
            init_timeout=config.gsm.init_timeout_s,
        )
    if name == "null":
        from trackify.notify.provider import NullProvider
        return NullProvider()
    from trackify.notify.provider import ConsoleProvider
    return ConsoleProvider()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TRACKIFY scan station")
    parser.add_argument("--windowed", action="store_true",
                        help="run in a window instead of fullscreen kiosk mode")
    parser.add_argument("--camera", default=None, metavar="NAME",
                        help="camera to use, matched on a substring of its name "
                             "(see: python scripts/check_camera.py --list)")
    parser.add_argument("--no-camera", action="store_true",
                        help="keyboard/HID-scanner input only")
    parser.add_argument("--custody", action="store_true",
                        help="open the custody desk instead of the scan station "
                             "(items held at the gate, releases, returns)")
    parser.add_argument("--provider", default="console",
                        choices=["console", "null", "gsm"],
                        help="notification provider (default: console, sends nothing)")
    args = parser.parse_args(argv)

    config = load_config()

    if args.no_camera or args.camera is not None:
        config = dataclasses.replace(config, camera=dataclasses.replace(
            config.camera,
            enabled=not args.no_camera,
            device=args.camera or config.camera.device,
        ))

    if not config.secrets.qr_secret:
        print("TRACKIFY_QR_SECRET is not set. Copy .env.example to .env and generate one:\n"
              '  python -c "import secrets; print(secrets.token_urlsafe(32))"',
              file=sys.stderr)
        return 1

    if not db.DEFAULT_DB.exists():
        print(f"No database at {db.DEFAULT_DB}.\n"
              "  Run: python scripts/seed_demo.py", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    if STYLE.exists():
        app.setStyleSheet(STYLE.read_text(encoding="utf8"))

    # UI-thread connection. The worker opens its own.
    conn = db.connect()
    db.init_db(conn)

    # The custody desk is a teacher at a cupboard, not a queue at a gate: no camera,
    # no SMS worker, nothing that belongs to the scan station.
    if args.custody:
        from trackify.ui.custody import CustodyWindow
        window = CustodyWindow(conn)
        window.show()
        return app.exec()

    service = ScanService(conn, config)

    window = KioskWindow(service, windowed=args.windowed)

    # --- SMS worker on its own thread -------------------------------------
    # The kiosk has to open in the morning whatever the hardware is doing. A provider
    # that cannot be built reports itself unavailable in the status bar instead; this
    # guard is for the unforeseen, since GsmProvider no longer raises for an absent
    # module. Null rather than Console on the way down: Console reports ok=True and
    # would mark parent notifications sent when nothing was sent.
    try:
        provider = build_provider(args.provider, config)
    except Exception as exc:  # noqa: BLE001 - the kiosk starts without SMS, never not at all
        print(f"[SMS] {args.provider} provider could not be created: {exc}\n"
              "      Starting without notifications; the status bar will show it.",
              file=sys.stderr)
        from trackify.notify.provider import NullProvider
        provider = NullProvider()

    thread = QThread()
    worker = SmsWorker(provider, config)
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    worker.stats_changed.connect(window.on_stats)
    worker.alarm.connect(window.on_alarm)
    thread.start()

    def shutdown():
        window.camera.shutdown()
        worker.stop_from_ui()
        thread.quit()
        # Bounded on purpose. If a send is still in flight when this elapses the row
        # stays 'sending' and reconcile_stale marks it 'unknown' on the next launch --
        # a human decision, which is the designed outcome for a message that may
        # already have left. Waiting indefinitely for a modem would be worse.
        thread.wait(3000)

    app.aboutToQuit.connect(shutdown)

    window.show()
    # After show(), so a camera that takes a second to warm up does so behind an
    # already-visible screen rather than a blank one.
    window.start_camera()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
