"""Student QR Generator -- Tkinter front end, the entry point for the .exe.

Tkinter rather than PySide6 on purpose: it ships with Python, so the frozen build
stays around 15 MB instead of pulling the whole Qt runtime in for four widgets.

Named main.py rather than app.py deliberately: the repo root already contains an
app.py (the kiosk), and once build.bat puts that root on the search path two
importable modules called "app" would collide.
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from generate import apply_changes, plan_changes, read_roster, read_secret

WINDOW_TITLE = "Student QR Generator"
# Log-line prefixes that mean a PNG was actually written or moved.
_WROTE_LABELS = {"NEW", "UPDATED", "REPAIR", "MOVED"}
DEFAULT_XLSX = "student-info.xlsx"
DEFAULT_OUT = "qr-out"


def _base_dir() -> Path:
    """Where the user thinks the program lives -- next to the exe once frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(WINDOW_TITLE)
        self.minsize(680, 460)

        base = _base_dir()
        default_xlsx = base / DEFAULT_XLSX
        self.xlsx = tk.StringVar(value=str(default_xlsx) if default_xlsx.is_file() else "")
        self.out_dir = tk.StringVar(value=str(base / DEFAULT_OUT))
        self.secret = tk.StringVar(value=read_secret(base))
        self.status = tk.StringVar(value="Ready.")

        self._messages: queue.Queue[str | tuple] = queue.Queue()
        self._running = False

        self._build_ui()
        self.secret.trace_add("write", lambda *_: self._refresh_button())
        self.xlsx.trace_add("write", lambda *_: self._refresh_button())
        self._refresh_button()
        self.after(80, self._drain)

    # ---------------------------------------------------------------- layout
    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Excel file:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.xlsx).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="Browse...", command=self._pick_xlsx).grid(row=0, column=2)

        ttk.Label(frame, text="Output folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.out_dir).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="Browse...", command=self._pick_out).grid(row=1, column=2)

        ttk.Label(frame, text="QR secret:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.secret, show="*").grid(
            row=2, column=1, sticky="ew", padx=6)
        ttk.Label(frame, text="TRACKIFY_QR_SECRET", foreground="#666").grid(
            row=2, column=2, sticky="w")

        ttk.Label(
            frame,
            text="This must be the SAME secret the kiosk runs with, or every printed "
                 "code will be rejected as forged.",
            foreground="#a00", wraplength=640, justify="left",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.generate_button = ttk.Button(frame, text="Generate", command=self._start)
        self.generate_button.grid(row=4, column=0, columnspan=3, pady=6)

        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        self.log = tk.Text(frame, height=14, wrap="none", state="disabled",
                           font=("Consolas", 9))
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(6, weight=1)

        scroll = ttk.Scrollbar(frame, command=self.log.yview)
        scroll.grid(row=6, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        ttk.Label(frame, textvariable=self.status).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

    # ---------------------------------------------------------------- events
    def _pick_xlsx(self) -> None:
        path = filedialog.askopenfilename(
            title="Select the student roster",
            filetypes=[("Excel workbook", "*.xlsx *.xlsm"), ("All files", "*.*")],
        )
        if path:
            self.xlsx.set(path)

    def _pick_out(self) -> None:
        path = filedialog.askdirectory(title="Where should the QR images go?")
        if path:
            self.out_dir.set(path)

    def _refresh_button(self) -> None:
        """No secret means no verifiable codes, so the button simply does not arm."""
        ready = bool(self.secret.get().strip()) and bool(self.xlsx.get().strip())
        self.generate_button.state(["!disabled"] if ready and not self._running
                                   else ["disabled"])
        if not self.secret.get().strip():
            self.status.set("Set the QR secret to continue.")
        elif not self._running:
            self.status.set("Ready.")

    def _write(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self) -> None:
        xlsx = Path(self.xlsx.get().strip())
        if not xlsx.is_file():
            messagebox.showerror(WINDOW_TITLE, f"Cannot find:\n{xlsx}")
            return

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        self._running = True
        self._refresh_button()
        self.status.set("Working...")
        threading.Thread(
            target=self._work,
            args=(xlsx, Path(self.out_dir.get().strip()), self.secret.get().strip()),
            daemon=True,
        ).start()

    # ------------------------------------------------------------ worker side
    def _work(self, xlsx: Path, out_dir: Path, secret: str) -> None:
        """Read the roster and work out what changed. Writes nothing.

        Runs off the UI thread and talks back only through the queue. The decision to
        write is taken on the UI thread, because it may need to ask a person first.
        """
        try:
            rows = read_roster(xlsx)
            self._messages.put(f"Read {len(rows)} student rows from {xlsx.name}")
            changes = plan_changes(rows, out_dir, secret)
            self._messages.put(("plan", changes, out_dir))
        except Exception as error:                      # noqa: BLE001 -- shown to user
            self._messages.put(("error", f"{type(error).__name__}: {error}"))

    def _apply(self, changes, out_dir: Path) -> None:
        """Second phase: actually write the images."""
        try:
            summary = apply_changes(changes, out_dir, self._messages.put)
            self._messages.put(("done", summary, out_dir))
        except Exception as error:                      # noqa: BLE001 -- shown to user
            self._messages.put(("error", f"{type(error).__name__}: {error}"))

    def _review(self, changes, out_dir: Path) -> None:
        """Show what changed, and for a repeat run let the person decide."""
        if changes.had_previous_run:
            self._write("")
            self._write(f"Compared against the last run in {out_dir.name}:")
            for label, count, note in (
                ("NEW", len(changes.new), "added to the roster"),
                ("UPDATED", len(changes.updated), "LRN changed -- REPRINT these"),
                ("MOVED", len(changes.moved), "renamed; same code, no reprint"),
                ("REPAIRED", len(changes.repaired), "image was missing"),
                ("REMOVED", len(changes.removed), "no longer in the roster"),
                ("SAME", len(changes.unchanged), "untouched, no reprint"),
            ):
                if count:
                    self._write(f"    {label:<9} {count:>4}   {note}")

            if changes.total_changes == 0:
                self._write("")
                self._write("Nothing changed. No files were written.")
                self._running = False
                self._refresh_button()
                self.status.set("Up to date -- nothing to do.")
                messagebox.showinfo(
                    WINDOW_TITLE,
                    f"The roster matches the codes already in:\n{out_dir}\n\n"
                    f"Nothing was written, so no card needs reprinting.")
                return

            reprint = len(changes.needs_reprint)
            if not messagebox.askokcancel(
                WINDOW_TITLE,
                f"{changes.total_changes} change(s) found.\n\n"
                f"  New       {len(changes.new)}\n"
                f"  Updated   {len(changes.updated)}"
                f"{'   <-- these cards must be reprinted' if reprint else ''}\n"
                f"  Renamed   {len(changes.moved)}\n"
                f"  Removed   {len(changes.removed)}\n"
                f"  Unchanged {len(changes.unchanged)}\n\n"
                f"Only the changed codes will be written. Continue?",
            ):
                self._write("")
                self._write("Cancelled. Nothing was written.")
                self._running = False
                self._refresh_button()
                self.status.set("Cancelled.")
                return

        self.progress.configure(
            maximum=max(len(changes.to_write) + len(changes.moved), 1), value=0)
        self.status.set("Writing...")
        threading.Thread(target=self._apply, args=(changes, out_dir),
                         daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                message = self._messages.get_nowait()
                if isinstance(message, str):
                    self._write(message)
                    # Advance only on lines that mean a file was touched, so the bar
                    # tracks real work rather than every note and skip.
                    if message.lstrip().split(" ")[0] in _WROTE_LABELS:
                        self.progress.step()
                elif message[0] == "plan":
                    self._review(message[1], message[2])
                elif message[0] == "done":
                    self._finish(message[1], message[2])
                elif message[0] == "error":
                    self._running = False
                    self._refresh_button()
                    self.status.set("Failed.")
                    self._write("ERROR: " + message[1])
                    messagebox.showerror(WINDOW_TITLE, message[1])
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _finish(self, summary, out_dir: Path) -> None:
        self._running = False
        self._refresh_button()

        self._write("")
        self._write("-" * 60)
        for section, count in summary.per_section.items():
            self._write(f"  {section:<20} {count} code(s)")
        self._write("-" * 60)
        self._write(f"  Codes now on file : {summary.total_codes}")
        self._write(f"  Written this run  : {summary.written}"
                    f"   (new {summary.new}, updated {summary.updated}, "
                    f"repaired {summary.repaired})")
        self._write(f"  Renamed           : {summary.moved}   (same code, no reprint)")
        self._write(f"  Unchanged         : {summary.unchanged}   (no reprint)")
        self._write(f"  Skipped           : {summary.skipped}   (no LRN)")
        if summary.removed:
            self._write(f"  Removed           : {summary.removed}   "
                        f"(off the roster; files left in place)")
        if summary.nonstandard:
            self._write(f"  Note              : {summary.nonstandard} LRN(s) are not "
                        f"12 digits; used exactly as typed")
        self._write("")
        self._write(f"  Images    : {out_dir}")
        self._write(f"  Manifest  : {summary.manifest_path.name}")
        self._write(f"  Changes   : {summary.changes_path.name}")
        self._write(f"  Report    : {summary.skipped_path.name}")
        self._write("")
        self._write("Print each code at least 25 mm wide. Matte, not glossy -- glare on")
        self._write("a laminated card is the usual reason a code will not scan.")

        self.status.set(
            f"Done. {summary.written} written, {summary.unchanged} unchanged, "
            f"{summary.total_codes} codes on file."
        )
        messagebox.showinfo(
            WINDOW_TITLE,
            f"{summary.written} code(s) written to:\n{out_dir}\n\n"
            f"{summary.total_codes} student(s) now have a code.\n"
            f"{summary.updated} card(s) must be REPRINTED (see changes.csv).\n"
            f"{summary.skipped} student(s) have no LRN (see skipped.csv).",
        )


def run_cli(argv: list[str]) -> int:
    """Headless generation. Kept because re-running after the adviser fills in the
    missing LRNs should not need a person clicking, and because it is the only way
    to exercise the frozen build without a desktop.

        qr-generator.exe --cli student-info.xlsx qr-out
    """
    import argparse

    parser = argparse.ArgumentParser(prog="qr-generator", description=__doc__)
    parser.add_argument("--cli", action="store_true", help="run without the window")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    parser.add_argument("xlsx", nargs="?", default=str(_base_dir() / DEFAULT_XLSX))
    parser.add_argument("out", nargs="?", default=str(_base_dir() / DEFAULT_OUT))
    args = parser.parse_args(argv)

    # A Windows console defaults to cp1252, which cannot encode the "n" with a caron
    # in "Seňora, Dave D." -- printing a roster name would then kill the run with
    # UnicodeEncodeError. Ask for UTF-8 and fall back to replacing the character.
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    # A --windowed exe has no stdout, so print() there goes nowhere. Everything is
    # collected and also written to run.log, which is the only output the frozen
    # build can actually show someone.
    lines: list[str] = []

    def emit(line: str = "") -> None:
        lines.append(line)
        if sys.stdout is not None:
            try:
                print(line)
            except UnicodeEncodeError:
                # Last resort if reconfigure was refused: never lose the run over a
                # character that cannot be displayed.
                print(line.encode("ascii", "replace").decode("ascii"))

    secret = read_secret(_base_dir())
    if not secret:
        emit("TRACKIFY_QR_SECRET is not set; codes would be unverifiable.")
        _write_log(Path(args.out), lines)
        return 2

    rows = read_roster(args.xlsx)
    emit(f"Read {len(rows)} student rows from {Path(args.xlsx).name}")

    changes = plan_changes(rows, args.out, secret)
    if changes.had_previous_run:
        emit("")
        emit("Compared against the last run:")
        for label, count, note in (
            ("NEW", len(changes.new), "added to the roster"),
            ("UPDATED", len(changes.updated), "LRN changed -- REPRINT these"),
            ("MOVED", len(changes.moved), "renamed; same code, no reprint"),
            ("REPAIRED", len(changes.repaired), "image was missing"),
            ("REMOVED", len(changes.removed), "no longer in the roster"),
            ("SAME", len(changes.unchanged), "untouched, no reprint"),
        ):
            if count:
                emit(f"    {label:<9} {count:>4}   {note}")
        if changes.total_changes == 0:
            emit("")
            emit("Nothing changed. No files written.")
        emit("")

    if args.dry_run:
        emit("Dry run: nothing was written.")
        _write_log(Path(args.out), lines)
        return 0

    summary = apply_changes(changes, args.out, emit)

    emit("-" * 60)
    for section, count in summary.per_section.items():
        emit(f"  {section:<20} {count} code(s)")
    emit("-" * 60)
    emit(f"  Codes now on file : {summary.total_codes}")
    emit(f"  Written this run  : {summary.written}"
         f"   (new {summary.new}, updated {summary.updated}, "
         f"repaired {summary.repaired})")
    emit(f"  Renamed           : {summary.moved}   (same code, no reprint)")
    emit(f"  Unchanged         : {summary.unchanged}   (no reprint)")
    emit(f"  Skipped           : {summary.skipped}   (no LRN)")
    if summary.removed:
        emit(f"  Removed           : {summary.removed}   "
             f"(off the roster; files left in place)")
    if summary.nonstandard:
        emit(f"  Note              : {summary.nonstandard} LRN(s) are not "
             f"12 digits; used exactly as typed")
    emit(f"  Output            : {args.out}")

    _write_log(Path(args.out), lines)
    return 0


def _write_log(out_dir: Path, lines: list[str]) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "run.log").write_text("\n".join(lines), encoding="utf8")
    except OSError:
        pass


if __name__ == "__main__":
    if "--cli" in sys.argv[1:]:
        raise SystemExit(run_cli(sys.argv[1:]))
    App().mainloop()
