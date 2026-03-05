import os
import sys
import re
import time
import base64
import threading
import subprocess
import shutil
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any

import serial
from serial.tools import list_ports

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import pyqtSlot, QObject, pyqtSignal, QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage
from PyQt6.QtWebChannel import QWebChannel


APP_TITLE = "MicroPython Studio (Qt + Monaco + Flasher)"
DEFAULT_REPL_BAUD = 115200

CHIP_RE = re.compile(r"Detecting chip type\.*\s*([A-Za-z0-9\-\_]+)", re.IGNORECASE)
MAC_RE = re.compile(r"MAC:\s*([0-9A-Fa-f:]{17})")
WRITE_PCT_RE = re.compile(r"\((\d+)\s*%\)")

BOOTMODE_ERR_RE = re.compile(r"wrong boot mode detected", re.IGNORECASE)
DOWNLOADMODE_HINT = (
    "Wrong boot mode — put device in DOWNLOAD mode: hold BOOT (IO0), tap RESET/EN, keep holding until it connects."
)


@dataclass
class DetectedDevice:
    port: str
    label: str
    chip: Optional[str]
    mac: Optional[str]
    vid: Optional[int]
    pid: Optional[int]


def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, rel)


def _norm_abs(p: str) -> str:
    return str(Path(p).expanduser().resolve())


def parse_esptool_probe(output: str) -> tuple[Optional[str], Optional[str]]:
    chip = None
    mac = None
    m = CHIP_RE.search(output or "")
    if m:
        chip = m.group(1).strip()
    m = MAC_RE.search(output or "")
    if m:
        mac = m.group(1).strip()
    return chip, mac


def parse_esptool_percent(line: str) -> Optional[int]:
    m = WRITE_PCT_RE.search(line or "")
    if not m:
        return None
    try:
        p = int(m.group(1))
        if 0 <= p <= 100:
            return p
    except Exception:
        return None
    return None


def resolve_esptool_cmd() -> List[str]:
    # Use interpreter running this app (venv-friendly and PyInstaller-friendly)
    return [sys.executable, "-m", "esptool"]


def build_device_label(port: str, port_info, chip: Optional[str], mac: Optional[str]) -> str:
    vid = getattr(port_info, "vid", None)
    pid = getattr(port_info, "pid", None)
    desc = getattr(port_info, "description", "") or ""
    mfg = getattr(port_info, "manufacturer", "") or ""

    parts = [chip if chip else "ESP device"]
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
    return f"{' - '.join(parts)} — {usb_str} ({port})" if usb_str else f"{' - '.join(parts)} ({port})"


def is_esp_candidate(port_info) -> bool:
    vid = getattr(port_info, "vid", None)
    desc = (getattr(port_info, "description", "") or "").lower()
    mfg = (getattr(port_info, "manufacturer", "") or "").lower()

    if vid == 0x303A:
        return True

    if vid in (0x10C4, 0x1A86, 0x0403):  # CP210x, CH340, FTDI
        if "cp210" in desc or "silicon labs" in mfg:
            return True
        if "ch340" in desc or "wch" in mfg:
            return True
        if "ftdi" in desc or "ftdi" in mfg:
            return True

    if "esp32" in desc:
        return True
    if "usb serial jtag" in desc:
        return True
    if "serial-jtag" in desc:
        return True

    return False


def probe_port_for_esp(port: str, port_info, timeout_s: float = 2.5) -> Optional[DetectedDevice]:
    cmd = resolve_esptool_cmd() + [
        "--chip", "auto",
        "--port", port,
        "--before", "default_reset",
        "--after", "hard_reset",
        "chip_id",
    ]

    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
            creationflags=creationflags,
        )
        out = proc.stdout or ""
        if proc.returncode == 0:
            chip, mac = parse_esptool_probe(out)
            label = build_device_label(port, port_info, chip, mac)
            return DetectedDevice(
                port=port,
                label=label,
                chip=chip,
                mac=mac,
                vid=getattr(port_info, "vid", None),
                pid=getattr(port_info, "pid", None),
            )
    except Exception:
        pass

    if is_esp_candidate(port_info):
        vid = getattr(port_info, "vid", None)
        pid = getattr(port_info, "pid", None)
        desc = getattr(port_info, "description", "") or ""
        mfg = getattr(port_info, "manufacturer", "") or ""

        usb_parts = []
        if mfg:
            usb_parts.append(mfg)
        if desc and desc not in usb_parts:
            usb_parts.append(desc)
        if vid is not None and pid is not None:
            usb_parts.append(f"VID:PID {vid:04X}:{pid:04X}")

        usb_str = " / ".join([s for s in usb_parts if s.strip()])
        label = f"ESP candidate (unprobed) — {usb_str} ({port})" if usb_str else f"ESP candidate (unprobed) ({port})"

        return DetectedDevice(
            port=port,
            label=label,
            chip=None,
            mac=None,
            vid=vid,
            pid=pid,
        )

    return None


class DebugPage(QWebEnginePage):
    consoleMessage = pyqtSignal(str)

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        self.consoleMessage.emit(f"{sourceID}:{lineNumber} {message}")


class ReplSession:
    """
    Minimal MicroPython raw-REPL executor + base64 file writer.
    Must be used from background threads.
    """
    def __init__(self):
        self.ser: Optional[serial.Serial] = None
        self.lock = threading.Lock()

    def is_connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def connect(self, port: str, baud: int) -> None:
        self.disconnect()
        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            timeout=0.25,
            write_timeout=2.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        time.sleep(0.08)

    def disconnect(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def _write(self, b: bytes) -> None:
        assert self.ser is not None
        self.ser.write(b)
        self.ser.flush()

    def _read_some(self, n=4096) -> bytes:
        assert self.ser is not None
        try:
            return self.ser.read(n)
        except Exception:
            return b""

    def _drain(self, seconds=0.25) -> bytes:
        assert self.ser is not None
        end = time.time() + seconds
        buf = bytearray()
        while time.time() < end:
            chunk = self._read_some(4096)
            if chunk:
                buf.extend(chunk)
                end = time.time() + 0.08
            else:
                time.sleep(0.01)
        return bytes(buf)

    def interrupt(self) -> None:
        if self.is_connected():
            self._write(b"\x03")  # Ctrl-C

    def enter_raw(self) -> None:
        self._write(b"\x01")  # Ctrl-A
        time.sleep(0.05)
        self._drain(0.2)

    def exit_raw(self) -> None:
        self._write(b"\x02")  # Ctrl-B
        time.sleep(0.05)
        self._drain(0.2)

    def exec_raw(self, code: str, timeout_s: float = 4.0, max_out: int = 200_000) -> str:
        with self.lock:
            if not self.is_connected():
                raise RuntimeError("REPL not connected")

            self.interrupt()
            time.sleep(0.03)
            self._drain(0.15)

            self.enter_raw()
            payload = code.encode("utf-8", errors="replace")
            self._write(payload)
            self._write(b"\x04")  # Ctrl-D

            end = time.time() + timeout_s
            buf = bytearray()
            while time.time() < end:
                chunk = self._read_some(4096)
                if chunk:
                    buf.extend(chunk)
                    if len(buf) > max_out:
                        buf = buf[-max_out:]
                else:
                    time.sleep(0.01)

            self.exit_raw()
            return buf.decode("utf-8", errors="replace")

    def write_remote_file_base64(self, remote_path: str, content: bytes, chunk_b64_chars: int = 1200) -> None:
        if not remote_path:
            raise ValueError("remote_path is empty")

        b64 = base64.b64encode(content).decode("ascii")
        chunks = [b64[i:i + chunk_b64_chars] for i in range(0, len(b64), chunk_b64_chars)]

        self.exec_raw("\n".join([
            "import ubinascii",
            f"p={remote_path!r}",
            "f=open(p,'wb')",
            "f.close()",
        ]), timeout_s=3.0)

        for c in chunks:
            snippet = "\n".join([
                "import ubinascii",
                f"f=open({remote_path!r},'ab')",
                f"f.write(ubinascii.a2b_base64({c!r}))",
                "f.close()",
            ])
            self.exec_raw(snippet, timeout_s=3.0)


def _tree_build(root: Path, max_nodes: int = 5000) -> Dict[str, Any]:
    count = 0

    def walk(p: Path) -> Dict[str, Any]:
        nonlocal count
        count += 1
        if count > max_nodes:
            return {"path": str(p), "name": p.name, "type": "dir", "children": []}

        if p.is_dir():
            children: List[Dict[str, Any]] = []
            try:
                for ch in p.iterdir():
                    if ch.name in {".git", "__pycache__", ".venv", "venv", "node_modules"}:
                        continue
                    children.append(walk(ch))
            except Exception:
                pass
            return {"path": str(p), "name": p.name or str(p), "type": "dir", "children": children}
        else:
            return {"path": str(p), "name": p.name, "type": "file"}

    return walk(root)


# ==========================
# Pyright LSP (stdio client)
# ==========================

def _find_pyright_langserver_cmd() -> Optional[List[str]]:
    exe = shutil.which("pyright-langserver")
    if exe:
        return [exe, "--stdio"]
    return [sys.executable, "-m", "pyright.langserver", "--stdio"]


class PyrightLspClient:
    def __init__(self, log_fn):
        self._log = log_fn
        self._proc: Optional[subprocess.Popen] = None
        self._wlock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._alive = threading.Event()

        self._next_id = 1
        self._id_lock = threading.Lock()

        self._initialized = False
        self._root_uri: Optional[str] = None
        self._docs: Dict[str, int] = {}

        self.on_diagnostics = None  # Optional[callable]

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._alive.is_set()

    def start(self, root_uri: Optional[str] = None) -> bool:
        if self.is_running():
            return True

        cmd = _find_pyright_langserver_cmd()
        if not cmd:
            self._log("[lsp] pyright-langserver not found (install: pip install pyright).")
            return False

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=0,
                creationflags=creationflags,
            )
            assert self._proc.stdin is not None
            assert self._proc.stdout is not None
        except Exception as e:
            self._proc = None
            self._log(f"[lsp] failed to start pyright langserver: {e!r}")
            return False

        self._alive.set()
        self._root_uri = root_uri

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        try:
            self._send_request("initialize", {
                "processId": os.getpid(),
                "rootUri": self._root_uri,
                "capabilities": {
                    "textDocument": {
                        "synchronization": {"didSave": True, "willSave": False, "willSaveWaitUntil": False},
                        "publishDiagnostics": {},
                    },
                    "workspace": {},
                },
                "clientInfo": {"name": "MicroPython Studio", "version": "1.0"},
            })
            self._send_notification("initialized", {})
            self._initialized = True
            self._log("[lsp] pyright language server started.")
            return True
        except Exception as e:
            self._log(f"[lsp] initialize failed: {e!r}")
            return False

    def stop(self):
        self._alive.clear()
        proc = self._proc
        self._proc = None
        self._initialized = False
        self._docs.clear()

        if proc is not None:
            try:
                try:
                    self._send_request("shutdown", {})
                    self._send_notification("exit", {})
                except Exception:
                    pass
                proc.terminate()
            except Exception:
                pass

    def _next_req_id(self) -> int:
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def _write_framed(self, payload: str):
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("LSP not running")
        data = payload.encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        with self._wlock:
            self._proc.stdin.buffer.write(header)
            self._proc.stdin.buffer.write(data)
            self._proc.stdin.buffer.flush()

    def _send_notification(self, method: str, params: Any):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        self._write_framed(json.dumps(msg))

    def _send_request(self, method: str, params: Any) -> int:
        rid = self._next_req_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._write_framed(json.dumps(msg))
        return rid

    def did_open(self, uri: str, text: str, language_id: str = "python"):
        if not self.is_running():
            self.start(root_uri=self._root_uri)
        if not self.is_running():
            return
        ver = self._docs.get(uri, 0) + 1
        self._docs[uri] = ver
        self._send_notification("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": language_id, "version": ver, "text": text or ""},
        })

    def did_change_full(self, uri: str, text: str):
        if not self.is_running():
            return
        ver = self._docs.get(uri, 0) + 1
        self._docs[uri] = ver
        self._send_notification("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": ver},
            "contentChanges": [{"text": text or ""}],
        })

    def did_close(self, uri: str):
        if not self.is_running():
            return
        self._send_notification("textDocument/didClose", {"textDocument": {"uri": uri}})
        self._docs.pop(uri, None)

    def _read_exact(self, n: int) -> bytes:
        assert self._proc is not None and self._proc.stdout is not None
        buf = b""
        while len(buf) < n and self._alive.is_set():
            chunk = self._proc.stdout.buffer.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _read_headers(self) -> Dict[str, str]:
        assert self._proc is not None and self._proc.stdout is not None
        headers: Dict[str, str] = {}
        while self._alive.is_set():
            line = self._proc.stdout.buffer.readline()
            if not line:
                break
            line = line.strip(b"\r\n")
            if not line:
                break
            try:
                k, v = line.decode("ascii", errors="ignore").split(":", 1)
                headers[k.strip().lower()] = v.strip()
            except Exception:
                continue
        return headers

    def _reader_loop(self):
        try:
            while self._alive.is_set() and self._proc and self._proc.poll() is None:
                headers = self._read_headers()
                if not headers:
                    time.sleep(0.02)
                    continue
                cl = headers.get("content-length")
                if not cl:
                    continue
                try:
                    n = int(cl)
                except Exception:
                    continue
                body = self._read_exact(n)
                if not body:
                    continue
                try:
                    msg = json.loads(body.decode("utf-8", errors="replace"))
                except Exception:
                    continue

                if isinstance(msg, dict) and msg.get("method") == "textDocument/publishDiagnostics":
                    params = msg.get("params") or {}
                    uri = str(params.get("uri") or "")
                    diagnostics = params.get("diagnostics") or []
                    if callable(self.on_diagnostics):
                        try:
                            self.on_diagnostics(uri, diagnostics)
                        except Exception:
                            pass
        except Exception as e:
            self._log(f"[lsp] reader loop error: {e!r}")
        finally:
            self._alive.clear()


class Bridge(QObject):
    """
    WebChannel bridge (thread-safe: worker threads only emit signals).
    """
    logMessage = pyqtSignal(str)
    deviceList = pyqtSignal(list)       # list[dict] for JS dropdown
    replStatus = pyqtSignal(bool, str)  # connected, message

    # Pyright LSP -> JS
    lspDiagnostics = pyqtSignal(str, object)  # (uri, diagnostics list)

    # Flasher -> JS
    # mode: idle | indeterminate | determinate | done | error
    flashStatus = pyqtSignal(str, str, int)   # (mode, text, percent)

    def __init__(self, repl: ReplSession, busy_flag: threading.Event):
        super().__init__()
        self.repl = repl
        self.busy_flag = busy_flag
        self._op_lock = threading.Lock()

        self._lsp = PyrightLspClient(self._emit)
        self._lsp.on_diagnostics = self._on_lsp_diagnostics

        self._closing = False
        try:
            self.destroyed.connect(lambda *_: setattr(self, "_closing", True))
        except Exception:
            pass

    def _emit(self, msg: str):
        if getattr(self, "_closing", False):
            return
        try:
            self.logMessage.emit(str(msg))
        except RuntimeError:
            return

    def _flash_emit(self, mode: str, text: str, percent: int = 0):
        if getattr(self, "_closing", False):
            return
        try:
            p = int(percent)
        except Exception:
            p = 0
        p = max(0, min(100, p))
        try:
            self.flashStatus.emit(str(mode), str(text), p)
        except RuntimeError:
            return

    def _on_lsp_diagnostics(self, uri: str, diagnostics: Any):
        self.lspDiagnostics.emit(uri, diagnostics)

    def _disconnect_repl_best_effort(self):
        try:
            if self.repl.is_connected():
                self._emit("[flash] disconnecting REPL…")
                try:
                    self.repl.disconnect()
                except Exception:
                    pass
                try:
                    self.replStatus.emit(False, "Disconnected (flash)")
                except Exception:
                    pass
        except Exception:
            pass

    def _run_esptool_stream(self, args: List[str], percent_hint: bool = True) -> int:
        pct_re = WRITE_PCT_RE

        self._emit("[flash] " + " ".join(args))
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            p = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as e:
            self._emit(f"[flash] failed to start esptool: {e!r}")
            return 127

        last_pct = -1
        boot_err = False

        try:
            assert p.stdout is not None
            for raw in p.stdout:
                line = raw.rstrip("\n")
                if line:
                    self._emit("[flash] " + line)

                if BOOTMODE_ERR_RE.search(line or ""):
                    boot_err = True
                    self._flash_emit("error", DOWNLOADMODE_HINT, 0)

                if percent_hint:
                    m = pct_re.search(line or "")
                    if m:
                        try:
                            pct = int(m.group(1))
                            if pct != last_pct:
                                last_pct = pct
                                self._flash_emit("determinate", "Flashing…", pct)
                        except Exception:
                            pass
        finally:
            rc = p.wait()

        # If boot mode error and failure, make it obvious
        if rc != 0 and boot_err:
            self._emit("[flash] hint: " + DOWNLOADMODE_HINT)

        return int(rc)

    # ---------------- Web UI logging ----------------

    @pyqtSlot(str)
    def js_log(self, msg: str):
        self._emit(f"[js] {msg}")

    @pyqtSlot(result=str)
    def ping(self) -> str:
        self._emit("[bridge] ping() called from JS")
        return "pong"

    # ---------------- Pyright LSP slots ----------------

    @pyqtSlot()
    def lsp_start(self):
        def worker():
            try:
                root = Path(os.getcwd()).resolve()
                root_uri = QUrl.fromLocalFile(str(root)).toString()
                ok = self._lsp.start(root_uri=root_uri)
                if not ok:
                    self._emit("[lsp] failed to start (install: pip install pyright).")
            except Exception as e:
                self._emit(f"[lsp] start error: {e!r}")
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot(str, str)
    def lsp_open(self, uri: str, text: str):
        def worker():
            try:
                if not self._lsp.is_running():
                    root = Path(os.getcwd()).resolve()
                    root_uri = QUrl.fromLocalFile(str(root)).toString()
                    self._lsp.start(root_uri=root_uri)
                self._lsp.did_open(str(uri), text or "", language_id="python")
            except Exception as e:
                self._emit(f"[lsp] open error: {e!r}")
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot(str, str)
    def lsp_change(self, uri: str, text: str):
        def worker():
            try:
                if not self._lsp.is_running():
                    return
                self._lsp.did_change_full(str(uri), text or "")
            except Exception as e:
                self._emit(f"[lsp] change error: {e!r}")
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot(str)
    def lsp_close(self, uri: str):
        def worker():
            try:
                if not self._lsp.is_running():
                    return
                self._lsp.did_close(str(uri))
            except Exception as e:
                self._emit(f"[lsp] close error: {e!r}")
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot()
    def lsp_stop(self):
        def worker():
            try:
                self._lsp.stop()
                self._emit("[lsp] stopped.")
            except Exception as e:
                self._emit(f"[lsp] stop error: {e!r}")
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- Device scan ----------------

    @pyqtSlot()
    def scan_devices(self):
        def worker():
            if self.busy_flag.is_set():
                self._emit("[scan] Busy (another operation in progress).")
                self.deviceList.emit([])
                return

            with self._op_lock:
                if self.repl.is_connected():
                    self._emit("[scan] disconnecting REPL to scan…")
                    try:
                        self.repl.disconnect()
                        self.replStatus.emit(False, "Disconnected (scan)")
                    except Exception:
                        pass

            self._emit("[scan] scanning for ESP devices…")
            ports = list(list_ports.comports())
            hits: List[DetectedDevice] = []
            for p in ports:
                dev = probe_port_for_esp(p.device, p, timeout_s=2.5)
                if dev is not None:
                    hits.append(dev)

            payload: List[Dict[str, str]] = [{
                "port": d.port,
                "label": d.label,
                "chip": d.chip or "",
                "mac": d.mac or "",
            } for d in hits]

            self.deviceList.emit(payload)
            self._emit(f"[scan] found {len(hits)} device(s).")

        threading.Thread(target=worker, daemon=True).start()

    # ---------------- Legacy file APIs ----------------

    @pyqtSlot(str, result=str)
    def load_file(self, path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8")
        except Exception as e:
            self._emit(f"[load_file error] {e!r}")
            return ""

    @pyqtSlot(str, str, result=bool)
    def save_file(self, path: str, content: str) -> bool:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            Path(path).write_text(content or "", encoding="utf-8", newline="\n")
            self._emit(f"Saved: {path}")
            return True
        except Exception as e:
            self._emit(f"[save_file error] {e!r}")
            return False

    # ---------------- New multi-file project APIs ----------------

    @pyqtSlot(result=str)
    def fs_get_cwd(self) -> str:
        return os.getcwd()

    @pyqtSlot(str, result="QVariant")
    def fs_tree(self, root: str):
        try:
            rp = Path(_norm_abs(str(Path(root).expanduser())))
            if not rp.exists():
                return {"path": str(rp), "name": rp.name or str(rp), "type": "dir", "children": []}
            return _tree_build(rp)
        except Exception as e:
            self._emit(f"[fs_tree error] {e!r}")
            return {"path": str(root), "name": str(root), "type": "dir", "children": []}

    @pyqtSlot(str, result=str)
    def fs_read(self, path: str) -> str:
        try:
            return Path(_norm_abs(path)).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self._emit(f"[fs_read error] {e!r}")
            return ""

    @pyqtSlot(str, str, result=bool)
    def fs_write(self, path: str, content: str) -> bool:
        try:
            p = Path(_norm_abs(path))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content or "", encoding="utf-8", newline="\n")
            return True
        except Exception as e:
            self._emit(f"[fs_write error] {e!r}")
            return False

    @pyqtSlot(str, result=bool)
    def fs_mkdir(self, path: str) -> bool:
        try:
            Path(_norm_abs(path)).mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            self._emit(f"[fs_mkdir error] {e!r}")
            return False

    @pyqtSlot(str, result=bool)
    def fs_delete(self, path: str) -> bool:
        try:
            p = Path(_norm_abs(path))
            if p.is_dir():
                shutil.rmtree(p)
            else:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            return True
        except Exception as e:
            self._emit(f"[fs_delete error] {e!r}")
            return False

    @pyqtSlot(str, str, result=bool)
    def project_generate(self, root: str, template: str) -> bool:
        try:
            rp = Path(_norm_abs(root))
            rp.mkdir(parents=True, exist_ok=True)

            def w(rel: str, txt: str):
                p = rp / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(txt, encoding="utf-8", newline="\n")

            if template == "mp_basic":
                w("main.py", "print('Hello from MicroPython project')\n")
                w("boot.py", "# boot.py\n")
                w("README.md", "# MicroPython Project\n\nGenerated by MicroPython Studio.\n")
            elif template == "mp_driver":
                w("main.py", "from drivers.example import Example\n\nex = Example()\nprint(ex.info())\n")
                w("drivers/example.py", "class Example:\n    def info(self):\n        return 'driver ok'\n")
                w("README.md", "# Driver Skeleton\n\nPut drivers in /drivers.\n")
            elif template == "mp_pkg":
                w("src/__init__.py", "")
                w("src/app.py", "def run():\n    print('app running')\n")
                w("main.py", "from src.app import run\nrun()\n")
                w("README.md", "# Package Layout\n\nmain.py imports from src/.\n")
            else:
                w("main.py", "print('New project')\n")

            self._emit(f"[project] generated template={template!r} at {str(rp)}")
            return True
        except Exception as e:
            self._emit(f"[project_generate error] {e!r}")
            return False

    # ---------------- REPL connect/run/push ----------------

    @pyqtSlot(str, int)
    def connect_repl(self, port: str, baud: int):
        def worker():
            with self._op_lock:
                try:
                    if self.busy_flag.is_set():
                        self.replStatus.emit(False, "Busy (flash/push running). Try again.")
                        return
                    self.repl.connect(port, baud)
                    self.replStatus.emit(True, f"Connected REPL: {port} @ {baud}")
                except Exception as e:
                    self.repl.disconnect()
                    self.replStatus.emit(False, f"Failed to connect: {e!r}")
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot()
    def disconnect_repl(self):
        def worker():
            with self._op_lock:
                try:
                    self.repl.disconnect()
                finally:
                    self.replStatus.emit(False, "Disconnected")
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot(str)
    def run_buffer(self, code: str):
        def worker():
            if self.busy_flag.is_set():
                self._emit("[run] Busy (another operation in progress).")
                return
            self.busy_flag.set()
            try:
                with self._op_lock:
                    if not self.repl.is_connected():
                        self._emit("[run] REPL not connected.")
                        return
                    self._emit("[run] executing…")
                    out = self.repl.exec_raw(code, timeout_s=6.0)
                self._emit(out if out.strip() else "[run] done.")
            except Exception as e:
                self._emit(f"[run] error: {e!r}")
            finally:
                self.busy_flag.clear()
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot(str, str)
    def push_file(self, remote_path: str, content: str):
        def worker():
            if self.busy_flag.is_set():
                self._emit("[push] Busy (another operation in progress).")
                return
            self.busy_flag.set()
            try:
                with self._op_lock:
                    if not self.repl.is_connected():
                        self._emit("[push] REPL not connected.")
                        return

                    payload = (content or "").encode("utf-8", errors="replace")
                    self._emit(f"[push] writing {remote_path!r} ({len(payload)/1024.0:.1f} KB)…")
                    self.repl.write_remote_file_base64(remote_path, payload, chunk_b64_chars=1200)

                self._emit(f"[push] done: {remote_path}")
            except Exception as e:
                self._emit(f"[push] error: {e!r}")
            finally:
                self.busy_flag.clear()
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- Flasher (called from editor.js) ----------------

    @pyqtSlot(result=str)
    def ui_pick_firmware(self) -> str:
        try:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                None,
                "Select MicroPython firmware (.bin)",
                os.getcwd(),
                "Firmware (*.bin);;All files (*)",
            )
            return path or ""
        except Exception as e:
            self._emit(f"[flash] ui_pick_firmware error: {e!r}")
            return ""

    @pyqtSlot(str, int)
    def flash_erase(self, port: str, baud: int):
        def worker():
            if self.busy_flag.is_set():
                self._emit("[flash] Busy (another operation in progress).")
                self._flash_emit("error", "Busy", 0)
                return

            self.busy_flag.set()
            self._flash_emit("indeterminate", "Erasing…", 0)
            try:
                with self._op_lock:
                    self._disconnect_repl_best_effort()
                    args = resolve_esptool_cmd() + [
                        "--chip", "auto",
                        "--port", str(port),
                        "--baud", str(int(baud) if baud else 460800),
                        "erase-flash",
                    ]
                    rc = self._run_esptool_stream(args, percent_hint=False)

                if rc == 0:
                    self._emit("[flash] erase complete.")
                    self._flash_emit("done", "Erase complete", 100)
                else:
                    self._emit(f"[flash] erase failed (rc={rc}).")
                    self._flash_emit("error", f"Erase failed (rc={rc})", 0)

            except Exception as e:
                self._emit(f"[flash] erase error: {e!r}")
                self._flash_emit("error", "Erase error", 0)
            finally:
                self.busy_flag.clear()
                self._flash_emit("idle", "Idle", 0)
        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot(str, str, int, str, bool)
    def flash_firmware(self, port: str, fw_path: str, baud: int, addr: str, erase_first: bool):
        def worker():
            if self.busy_flag.is_set():
                self._emit("[flash] Busy (another operation in progress).")
                self._flash_emit("error", "Busy", 0)
                return

            fw = str(fw_path or "")
            if not fw or not Path(fw).exists():
                self._emit(f"[flash] firmware not found: {fw!r}")
                self._flash_emit("error", "Firmware not found", 0)
                return

            self.busy_flag.set()
            self._flash_emit("indeterminate", "Starting…", 0)

            try:
                with self._op_lock:
                    self._disconnect_repl_best_effort()

                    baud_i = int(baud) if baud else 460800
                    addr_s = str(addr or "0x1000").strip() or "0x1000"

                    if erase_first:
                        self._emit("[flash] erase-flash…")
                        self._flash_emit("indeterminate", "Erasing…", 0)
                        rc_erase = self._run_esptool_stream(
                            resolve_esptool_cmd() + [
                                "--chip", "auto",
                                "--port", str(port),
                                "--baud", str(baud_i),
                                "erase-flash",
                            ],
                            percent_hint=False,
                        )
                        if rc_erase != 0:
                            self._emit(f"[flash] erase failed (rc={rc_erase}).")
                            self._flash_emit("error", f"Erase failed (rc={rc_erase})", 0)
                            return

                    self._emit("[flash] write-flash…")
                    self._flash_emit("indeterminate", "Flashing…", 0)
                    rc = self._run_esptool_stream(
                        resolve_esptool_cmd() + [
                            "--chip", "auto",
                            "--port", str(port),
                            "--baud", str(baud_i),
                            "write-flash",
                            "-z",
                            addr_s,
                            fw,
                        ],
                        percent_hint=True,
                    )

                if rc == 0:
                    self._emit("[flash] flash complete.")
                    self._flash_emit("done", "Flash complete", 100)
                else:
                    self._emit(f"[flash] flash failed (rc={rc}).")
                    self._flash_emit("error", f"Flash failed (rc={rc})", 0)

            except Exception as e:
                self._emit(f"[flash] flash error: {e!r}")
                self._flash_emit("error", "Flash error", 0)
            finally:
                self.busy_flag.clear()
                self._flash_emit("idle", "Idle", 0)
        threading.Thread(target=worker, daemon=True).start()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1250, 820)

        self.busy_flag = threading.Event()
        self.repl = ReplSession()
        self.bridge = Bridge(self.repl, self.busy_flag)

        splitter = QtWidgets.QSplitter()
        splitter.setOrientation(QtCore.Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        # Left: Web editor
        self.view = QWebEngineView()
        self.page = DebugPage(self.view)
        self.view.setPage(self.page)

        s = self.view.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        splitter.addWidget(self.view)

        # Right: Qt log (debug)
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(8000)
        right_layout.addWidget(self.log, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        # WebChannel hookup (keep persistent references!)
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        # Signals
        self.bridge.logMessage.connect(self._append_log)
        self.bridge.replStatus.connect(lambda c, m: self._append_log("[repl] " + m))
        self.bridge.deviceList.connect(lambda ds: self._append_log(f"[scan] deviceList count={len(ds or [])}"))
        self.page.consoleMessage.connect(lambda m: self._append_log(f"[console] {m}"))

        # Load editor
        html_path = os.path.abspath(resource_path(os.path.join("web", "editor.html")))
        self._append_log(f"Loading editor: {html_path}")
        self.view.load(QUrl.fromLocalFile(html_path))

        # initial scan
        QtCore.QTimer.singleShot(700, self.bridge.scan_devices)

        # helpful: page load proof in Qt log
        self.view.loadFinished.connect(lambda ok: self._append_log(f"[web] loadFinished ok={ok}"))

    def closeEvent(self, event):
        try:
            try:
                self.bridge._closing = True
            except Exception:
                pass
            try:
                self.bridge.lsp_stop()
            except Exception:
                pass
            try:
                self.bridge.disconnect_repl()
            except Exception:
                pass
        finally:
            super().closeEvent(event)

    def _append_log(self, msg: str):
        self.log.appendPlainText(str(msg).rstrip("\n"))


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()

    def _on_quit():
        try:
            w.bridge._closing = True
        except Exception:
            pass
        try:
            w.bridge.lsp_stop()
        except Exception:
            pass
        try:
            w.bridge.disconnect_repl()
        except Exception:
            pass

    app.aboutToQuit.connect(_on_quit)

    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()