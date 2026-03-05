import os
import sys
import re
import threading
import subprocess
import queue
import time
from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import serial
from serial.tools import list_ports


APP_TITLE = "MicroPython Flasher + REPL (ESP)"
DEFAULT_FLASH_BAUD = "460800"
DEFAULT_REPL_BAUD = "115200"
DEFAULT_FLASH_ADDR = "0x1000"

CHIP_RE = re.compile(r"Detecting chip type\.*\s*([A-Za-z0-9\-\_]+)", re.IGNORECASE)
MAC_RE = re.compile(r"MAC:\s*([0-9A-Fa-f:]{17})")


@dataclass
class DetectedDevice:
    port: str
    label: str          # user-facing
    chip: str | None    # parsed
    mac: str | None     # parsed
    vid: int | None
    pid: int | None


def resource_path(rel: str) -> str:
    """
    PyInstaller-friendly resource resolver.
    """
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, rel)


def parse_esptool_probe(output: str) -> tuple[str | None, str | None]:
    chip = None
    mac = None

    m = CHIP_RE.search(output)
    if m:
        chip = m.group(1).strip()

    m = MAC_RE.search(output)
    if m:
        mac = m.group(1).strip()

    return chip, mac


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x620")
        self.minsize(900, 560)

        # Process log queue (esptool)
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.worker: threading.Thread | None = None

        # Device selection (ESP probe-based)
        self.devices: list[DetectedDevice] = []
        self.device_var = tk.StringVar(value="")
        self.selected_device: DetectedDevice | None = None

        # REPL
        self.repl_q: "queue.Queue[str]" = queue.Queue()
        self.ser: serial.Serial | None = None
        self.repl_reader_thread: threading.Thread | None = None
        self.repl_running = False

        self._build_ui()
        self._scan_devices_clicked()

        self._poll_log_queue()
        self._poll_repl_queue()

        # Optional: if you ship a default firmware in assets/
        default_fw = resource_path(os.path.join("assets", "firmware.bin"))
        if os.path.exists(default_fw):
            self.fw_path.set(default_fw)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI ----------------

    def _build_ui(self):
        pad = 10

        # Top device selector bar
        top = ttk.Frame(self)
        top.pack(fill="x", padx=pad, pady=(pad, 0))

        ttk.Label(top, text="Detected device:").grid(row=0, column=0, sticky="w")
        self.device_combo = ttk.Combobox(top, textvariable=self.device_var, width=72, state="readonly")
        self.device_combo.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(6, 0))
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Button(top, text="Scan Devices", command=self._scan_devices_clicked).grid(
            row=0, column=4, sticky="w", padx=(8, 0)
        )

        top.columnconfigure(0, weight=0)
        top.columnconfigure(1, weight=1)
        top.columnconfigure(2, weight=0)
        top.columnconfigure(3, weight=0)
        top.columnconfigure(4, weight=0)

        # Tabs
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=pad, pady=pad)

        self.tab_flash = ttk.Frame(self.nb)
        self.tab_repl = ttk.Frame(self.nb)

        self.nb.add(self.tab_flash, text="Flasher")
        self.nb.add(self.tab_repl, text="REPL")

        self._build_flash_tab(self.tab_flash, pad)
        self._build_repl_tab(self.tab_repl, pad)

    def _build_flash_tab(self, parent: ttk.Frame, pad: int):
        form = ttk.Frame(parent)
        form.pack(fill="x", padx=0, pady=(0, 10))

        # Flash baud
        ttk.Label(form, text="Flash baud:").grid(row=0, column=0, sticky="w", pady=(6, 0))
        self.flash_baud_var = tk.StringVar(value=DEFAULT_FLASH_BAUD)
        self.flash_baud_combo = ttk.Combobox(
            form, textvariable=self.flash_baud_var, width=12, state="readonly",
            values=["115200", "230400", "460800", "921600", "1500000"]
        )
        self.flash_baud_combo.grid(row=0, column=1, sticky="w", padx=(6, 0), pady=(6, 0))

        # Firmware
        ttk.Label(form, text="Firmware (.bin):").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.fw_path = tk.StringVar(value="")
        fw_entry = ttk.Entry(form, textvariable=self.fw_path)
        fw_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=(10, 0))
        ttk.Button(form, text="Browse…", command=self._browse_fw).grid(row=1, column=4, sticky="w", padx=(6, 0), pady=(10, 0))

        # Flash address
        ttk.Label(form, text="Flash address:").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.addr_var = tk.StringVar(value=DEFAULT_FLASH_ADDR)
        ttk.Entry(form, textvariable=self.addr_var, width=12).grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(10, 0))

        # Options
        self.erase_first = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Erase flash before writing", variable=self.erase_first).grid(
            row=2, column=2, columnspan=3, sticky="w", padx=(8, 0), pady=(10, 0)
        )

        form.columnconfigure(0, weight=0)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(2, weight=0)
        form.columnconfigure(3, weight=0)
        form.columnconfigure(4, weight=0)

        # Buttons
        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=0, pady=(0, 10))

        self.flash_btn = ttk.Button(btns, text="Flash Firmware", command=self._flash_clicked)
        self.flash_btn.pack(side="left")

        self.erase_btn = ttk.Button(btns, text="Erase Flash Only", command=self._erase_clicked)
        self.erase_btn.pack(side="left", padx=(8, 0))

        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop_clicked, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        ttk.Button(btns, text="Clear Log", command=self._clear_flash_log).pack(side="right")

        # Log box
        log_frame = ttk.Frame(parent)
        log_frame.pack(fill="both", expand=True)

        self.flash_log = tk.Text(log_frame, wrap="word", height=10)
        self.flash_log.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.flash_log.yview)
        scroll.pack(side="right", fill="y")
        self.flash_log.configure(yscrollcommand=scroll.set)

        self._flash_log_line("Ready.")

    def _build_repl_tab(self, parent: ttk.Frame, pad: int):
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 10))

        ttk.Label(top, text="REPL baud:").pack(side="left")
        self.repl_baud_var = tk.StringVar(value=DEFAULT_REPL_BAUD)
        self.repl_baud_combo = ttk.Combobox(
            top, textvariable=self.repl_baud_var, width=12, state="readonly",
            values=["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
        )
        self.repl_baud_combo.pack(side="left", padx=(6, 10))

        self.repl_connect_btn = ttk.Button(top, text="Connect", command=self._repl_connect_clicked)
        self.repl_connect_btn.pack(side="left")

        self.repl_disconnect_btn = ttk.Button(top, text="Disconnect", command=self._repl_disconnect_clicked, state="disabled")
        self.repl_disconnect_btn.pack(side="left", padx=(8, 0))

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=10, pady=2)

        # Common control buttons
        ttk.Button(top, text="Ctrl-C (Interrupt)", command=lambda: self._repl_send_bytes(b"\x03")).pack(side="left")
        ttk.Button(top, text="Ctrl-D (Soft Reset)", command=lambda: self._repl_send_bytes(b"\x04")).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Ctrl-A (Raw REPL)", command=lambda: self._repl_send_bytes(b"\x01")).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Ctrl-B (Normal REPL)", command=lambda: self._repl_send_bytes(b"\x02")).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Clear", command=self._clear_repl_output).pack(side="right")

        # Output
        out_frame = ttk.Frame(parent)
        out_frame.pack(fill="both", expand=True)

        self.repl_out = tk.Text(out_frame, wrap="word", height=10)
        self.repl_out.pack(side="left", fill="both", expand=True)

        out_scroll = ttk.Scrollbar(out_frame, orient="vertical", command=self.repl_out.yview)
        out_scroll.pack(side="right", fill="y")
        self.repl_out.configure(yscrollcommand=out_scroll.set)

        # Input area
        inp = ttk.Frame(parent)
        inp.pack(fill="x", pady=(10, 0))

        ttk.Label(inp, text="Send:").pack(side="left")

        self.repl_lineend_var = tk.StringVar(value="\\r\\n")
        self.repl_lineend_combo = ttk.Combobox(
            inp, textvariable=self.repl_lineend_var, width=6, state="readonly",
            values=["\\n", "\\r\\n", ""]
        )
        self.repl_lineend_combo.pack(side="left", padx=(6, 10))
        ttk.Label(inp, text="Line ending").pack(side="left")

        self.repl_send_entry = ttk.Entry(inp)
        self.repl_send_entry.pack(side="left", fill="x", expand=True, padx=(10, 10))
        self.repl_send_entry.bind("<Return>", lambda _e: self._repl_send_line_clicked())

        self.repl_send_btn = ttk.Button(inp, text="Send", command=self._repl_send_line_clicked, state="disabled")
        self.repl_send_btn.pack(side="left")

        self._repl_out_line("REPL ready. Click Connect (device must be selected above).")

    # ---------------- Common helpers ----------------

    def _flash_log_line(self, s: str):
        self.flash_log.insert("end", s + "\n")
        self.flash_log.see("end")

    def _repl_out_line(self, s: str):
        self.repl_out.insert("end", s + "\n")
        self.repl_out.see("end")

    def _clear_flash_log(self):
        self.flash_log.delete("1.0", "end")

    def _clear_repl_output(self):
        self.repl_out.delete("1.0", "end")

    def _browse_fw(self):
        p = filedialog.askopenfilename(
            title="Select MicroPython firmware .bin",
            filetypes=[("Firmware", "*.bin"), ("All files", "*.*")]
        )
        if p:
            self.fw_path.set(p)

    def _on_device_selected(self, _evt=None):
        label = self.device_var.get()
        for d in self.devices:
            if d.label == label:
                self.selected_device = d
                # If REPL connected to something else, keep it; but most users expect disconnect on change.
                return
        self.selected_device = None

    def _validate_device_selected(self) -> tuple[bool, str]:
        if self.selected_device is None:
            return False, "No ESP device selected. Click 'Scan Devices' and pick one."
        return True, ""

    def _resolve_esptool_cmd(self) -> list[str]:
        """
        Use `python -m esptool` so it works even when packaged.
        """
        return [sys.executable, "-m", "esptool"]

    # ---------------- Process runner (esptool) ----------------

    def _set_flash_running(self, running: bool):
        self.flash_btn.configure(state="disabled" if running else "normal")
        self.erase_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

        # avoid fighting over the COM port
        if running and self.ser is not None:
            self._repl_out_line("[info] Flash started; disconnecting REPL to free the port.")
            self._repl_disconnect_clicked()

    def _start_process(self, args: list[str], title: str):
        if self.proc is not None:
            messagebox.showwarning(APP_TITLE, "A process is already running.")
            return

        self._flash_log_line("")
        self._flash_log_line(f"== {title} ==")
        self._flash_log_line("Command: " + " ".join(args))
        self._set_flash_running(True)

        def run():
            try:
                creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW on Windows
                self.proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=creationflags,
                )
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    self.log_q.put(line.rstrip("\n"))
                rc = self.proc.wait()
                self.log_q.put(f"[exit code] {rc}")
            except Exception as e:
                self.log_q.put(f"[error] {e!r}")
            finally:
                self.proc = None
                self.log_q.put("__DONE__")

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                if msg == "__DONE__":
                    self._set_flash_running(False)
                    self._flash_log_line("Done.")
                else:
                    self._flash_log_line(msg)
        except queue.Empty:
            pass
        self.after(80, self._poll_log_queue)

    def _stop_clicked(self):
        if self.proc is None:
            return
        try:
            self._flash_log_line("Stopping...")
            self.proc.terminate()
        except Exception as e:
            self._flash_log_line(f"[stop error] {e!r}")

    # ---------------- Device scanning / probing ----------------

    def _scan_devices_clicked(self):
        if self.proc is not None:
            messagebox.showwarning(APP_TITLE, "Please stop flashing before scanning.")
            return

        # scanning is fine while REPL is connected, but probing the same port can fail;
        # simplest: if connected, ask user to disconnect.
        if self.ser is not None:
            # keep it simple: disconnect to scan reliably
            self._repl_out_line("[info] Disconnecting REPL to scan devices.")
            self._repl_disconnect_clicked()

        self.devices = []
        self.selected_device = None
        self.device_combo["values"] = []
        self.device_var.set("")
        self._flash_log_line("Scanning ports for ESP devices...")

        ports = list(list_ports.comports())
        if not ports:
            self._flash_log_line("No serial ports found.")
            return

        def worker():
            hits: list[DetectedDevice] = []
            for p in ports:
                dev = self._probe_port_for_esp(p.device, p)
                if dev is not None:
                    hits.append(dev)
            self.after(0, lambda: self._apply_scan_results(hits))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_scan_results(self, hits: list[DetectedDevice]):
        self.devices = hits
        if not hits:
            self._flash_log_line("No ESP devices responded to probe.")
            return

        labels = [d.label for d in hits]
        self.device_combo["values"] = labels
        self.device_var.set(labels[0])
        self.selected_device = hits[0]

        self._flash_log_line(f"Found {len(hits)} ESP device(s):")
        for d in hits:
            self._flash_log_line(f" - {d.label}")

    def _probe_port_for_esp(self, port: str, port_info) -> DetectedDevice | None:
        """
        Try: python -m esptool --port COMx chip_id
        Returns a DetectedDevice if it looks like an ESP response.
        """
        cmd = self._resolve_esptool_cmd() + [
            "--port", port,
            "--chip", "auto",
            "chip_id",
        ]
        try:
            creationflags = 0x08000000 if os.name == "nt" else 0
            p = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=2.5,
                creationflags=creationflags,
            )
            out = p.stdout or ""
            if p.returncode != 0:
                return None

            chip, mac = parse_esptool_probe(out)

            # Some versions may not print "Detecting chip type" on chip_id,
            # but they still indicate success with esptool output.
            if not chip and "esptool" not in out.lower():
                return None

            vid = getattr(port_info, "vid", None)
            pid = getattr(port_info, "pid", None)
            desc = getattr(port_info, "description", "") or ""
            mfg = getattr(port_info, "manufacturer", "") or ""

            parts = []
            parts.append(chip if chip else "ESP device")
            if mac:
                parts.append(mac)

            usb = []
            if mfg:
                usb.append(mfg)
            if desc and desc not in usb:
                usb.append(desc)
            if vid is not None and pid is not None:
                usb.append(f"VID:PID {vid:04X}:{pid:04X}")

            usb_str = " / ".join([s for s in usb if s.strip()])
            if usb_str:
                label = f"{' - '.join(parts)} — {usb_str} ({port})"
            else:
                label = f"{' - '.join(parts)} ({port})"

            return DetectedDevice(
                port=port,
                label=label,
                chip=chip,
                mac=mac,
                vid=vid,
                pid=pid,
            )
        except Exception:
            return None

    # ---------------- Flash actions ----------------

    def _erase_clicked(self):
        ok, err = self._validate_device_selected()
        if not ok:
            messagebox.showerror(APP_TITLE, err)
            return

        port = self.selected_device.port
        baud = self.flash_baud_var.get().strip()

        args = self._resolve_esptool_cmd() + [
            "--chip", "auto",
            "--port", port,
            "--baud", baud,
            "erase_flash",
        ]
        self._start_process(args, "Erase Flash")

    def _flash_clicked(self):
        ok, err = self._validate_device_selected()
        if not ok:
            messagebox.showerror(APP_TITLE, err)
            return

        fw = self.fw_path.get().strip()
        if not fw or not os.path.exists(fw):
            messagebox.showerror(APP_TITLE, "Select a valid firmware .bin file.")
            return

        addr = self.addr_var.get().strip() or DEFAULT_FLASH_ADDR
        port = self.selected_device.port
        baud = self.flash_baud_var.get().strip()

        if self.erase_first.get():
            self._start_process(self._make_chained_erase_write_cmd(port, baud, addr, fw), "Erase + Write Flash")
        else:
            args = self._resolve_esptool_cmd() + [
                "--chip", "auto",
                "--port", port,
                "--baud", baud,
                "write_flash",
                "-z",
                addr,
                fw,
            ]
            self._start_process(args, "Write Flash")

    def _make_chained_erase_write_cmd(self, port: str, baud: str, addr: str, fw: str) -> list[str]:
        """
        Run a small Python one-liner that:
          1) python -m esptool ... erase_flash
          2) python -m esptool ... write_flash -z addr fw
        Avoids shell operators differences.
        """
        code = (
            "import subprocess, sys;"
            "def r(a):"
            "    p=subprocess.run(a);"
            "    sys.exit(p.returncode) if p.returncode else None;"
            "exe=sys.executable;"
            "r([exe,'-m','esptool','--chip','auto','--port',sys.argv[1],'--baud',sys.argv[2],'erase_flash']);"
            "r([exe,'-m','esptool','--chip','auto','--port',sys.argv[1],'--baud',sys.argv[2],'write_flash','-z',sys.argv[3],sys.argv[4]]);"
        )
        return [sys.executable, "-c", code, port, baud, addr, fw]

    # ---------------- REPL ----------------

    def _set_repl_connected(self, connected: bool):
        self.repl_connect_btn.configure(state="disabled" if connected else "normal")
        self.repl_disconnect_btn.configure(state="normal" if connected else "disabled")
        self.repl_send_btn.configure(state="normal" if connected else "disabled")

        # keep scanning/flashing safe:
        if connected:
            # prevent flashing without explicit action? (We already disconnect on flash start.)
            pass

    def _repl_connect_clicked(self):
        if self.proc is not None:
            messagebox.showwarning(APP_TITLE, "Stop flashing before connecting to REPL.")
            return

        ok, err = self._validate_device_selected()
        if not ok:
            messagebox.showerror(APP_TITLE, err)
            return

        if self.ser is not None:
            return

        port = self.selected_device.port
        try:
            baud = int(self.repl_baud_var.get().strip())
        except Exception:
            messagebox.showerror(APP_TITLE, "Invalid REPL baud rate.")
            return

        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,          # non-blocking-ish
                write_timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except Exception as e:
            self.ser = None
            messagebox.showerror(APP_TITLE, f"Failed to open {port}: {e}")
            return

        self.repl_running = True
        self.repl_reader_thread = threading.Thread(target=self._repl_reader_loop, daemon=True)
        self.repl_reader_thread.start()

        self._set_repl_connected(True)
        self._repl_out_line(f"[connected] {self.selected_device.label}")
        self._repl_out_line("Tip: Press Ctrl-D for soft reset if you don't see a prompt.")
        self.repl_send_entry.focus_set()

    def _repl_disconnect_clicked(self):
        if self.ser is None:
            return
        self.repl_running = False
        try:
            # give reader a moment to exit
            time.sleep(0.05)
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        self._set_repl_connected(False)
        self._repl_out_line("[disconnected]")

    def _repl_reader_loop(self):
        """
        Reads bytes from serial and pushes text to repl_q.
        """
        assert self.ser is not None
        ser = self.ser
        buf = bytearray()

        while self.repl_running and ser is self.ser:
            try:
                data = ser.read(4096)
                if data:
                    buf.extend(data)
                    # flush on newlines or if buffer gets big
                    if b"\n" in buf or len(buf) > 2048:
                        try:
                            text = buf.decode("utf-8", errors="replace")
                        except Exception:
                            text = buf.decode(errors="replace")
                        buf.clear()
                        self.repl_q.put(text)
                else:
                    # idle: flush partial buffer occasionally
                    if buf:
                        try:
                            text = buf.decode("utf-8", errors="replace")
                        except Exception:
                            text = buf.decode(errors="replace")
                        buf.clear()
                        self.repl_q.put(text)
                    time.sleep(0.02)
            except Exception as e:
                self.repl_q.put(f"\n[repl error] {e}\n")
                break

        # ensure cleaned up on exit
        self.repl_q.put("__REPL_DONE__")

    def _poll_repl_queue(self):
        try:
            while True:
                msg = self.repl_q.get_nowait()
                if msg == "__REPL_DONE__":
                    # Reader ended; if serial still open, mark disconnected
                    if self.ser is not None and not self.repl_running:
                        # already disconnected intentionally
                        pass
                    elif self.ser is not None and not self.repl_running:
                        pass
                else:
                    self._repl_append_text(msg)
        except queue.Empty:
            pass
        self.after(40, self._poll_repl_queue)

    def _repl_append_text(self, text: str):
        # Insert raw output without forcing extra newlines
        self.repl_out.insert("end", text)
        self.repl_out.see("end")

    def _repl_send_bytes(self, b: bytes):
        if self.ser is None:
            return
        try:
            self.ser.write(b)
            self.ser.flush()
        except Exception as e:
            self._repl_out_line(f"[send error] {e}")

    def _repl_send_line_clicked(self):
        if self.ser is None:
            return
        s = self.repl_send_entry.get()
        if s is None:
            return

        line_ending = self.repl_lineend_var.get()
        if line_ending == "\\n":
            s2 = s + "\n"
        elif line_ending == "\\r\\n":
            s2 = s + "\r\n"
        else:
            s2 = s

        try:
            self.ser.write(s2.encode("utf-8", errors="replace"))
            self.ser.flush()
            self.repl_send_entry.delete(0, "end")
        except Exception as e:
            self._repl_out_line(f"[send error] {e}")

    # ---------------- Close / cleanup ----------------

    def _on_close(self):
        try:
            if self.proc is not None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            if self.ser is not None:
                self._repl_disconnect_clicked()
        finally:
            self.destroy()


if __name__ == "__main__":
    # Better DPI scaling on Windows
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = App()
    app.mainloop()