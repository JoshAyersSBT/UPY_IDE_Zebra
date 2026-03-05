// web/editor.js
// Regenerated with:
// - Terminal output buffering (so early logs don't get dropped)
// - WebChannel attach proof (JS->Py and Py->JS via ping())
// - Guarded Monaco init to prevent duplicate module definition
// - Keeps: flasher UI wiring, LSP glue, project tree, REPL/log tabs, splitter, run spinner

let bridge = null;
let editor = null;

let lastDevices = [];
let selectedPort = "";
let connected = false;

let projectRoot = "";
let projectTree = null; // {path, name, type, children?}
let expanded = new Set(); // paths
let selectedNode = null; // {path,type,...}
let activeFilePath = ""; // full path on disk

function $(id) {
  const el = document.getElementById(id);
  if (!el) throw new Error(`Missing element #${id}`);
  return el;
}

function setStatus(s) {
  try { $("status").textContent = s; } catch {}
}

function setActiveFileLabel(path) {
  activeFilePath = path || "";
  const lbl = $("activeFileLabel");
  lbl.textContent = activeFilePath ? activeFilePath : "(no file)";
  lbl.title = activeFilePath ? activeFilePath : "No file open";
}

// -----------------------
// Flasher UI state + helpers (moved into WebView)
// -----------------------
let flashUiBusy = false;

function flashSetControlsEnabled(enabled) {
  const ids = [
    "flashFwPath",
    "btnBrowseFw",
    "flashBaud",
    "flashAddr",
    "flashEraseFirst",
    "btnFlashFirmware",
    "btnEraseFlash",
  ];
  for (const id of ids) {
    try { $(id).disabled = !enabled; } catch {}
  }
  flashUiBusy = !enabled;
}

function flashSetProgress(mode, text, percent) {
  // mode: idle | indeterminate | determinate | done | error
  try { $("flashStatus").textContent = text || ""; } catch {}

  let outer = null, inner = null, pctEl = null;
  try { outer = $("flashProgressOuter"); } catch {}
  try { inner = $("flashProgressInner"); } catch {}
  try { pctEl = $("flashProgressPct"); } catch {}
  if (!outer || !inner || !pctEl) return;

  outer.classList.toggle("indeterminate", mode === "indeterminate");

  if (mode === "determinate") {
    const p = Math.max(0, Math.min(100, Number(percent ?? 0)));
    inner.style.width = `${p}%`;
    pctEl.textContent = `${p}%`;
    return;
  }
  if (mode === "done") {
    inner.style.width = "100%";
    pctEl.textContent = "100%";
    return;
  }

  inner.style.width = "0%";
  pctEl.textContent = "0%";
}

// -----------------------
// Spinner in status pill during run_buffer
// -----------------------
let runInFlight = false;
let spinnerTimer = null;
let spinnerIndex = 0;
const SPINNER_FRAMES = ["|", "/", "-", "\\"];

function startRunSpinner(label = "Executing") {
  stopRunSpinner(false);
  runInFlight = true;
  spinnerIndex = 0;

  const tick = () => {
    const frame = SPINNER_FRAMES[spinnerIndex++ % SPINNER_FRAMES.length];
    setStatus(`${frame} ${label}…`);
  };

  tick();
  spinnerTimer = setInterval(tick, 90);
}

function stopRunSpinner(restore = true) {
  if (spinnerTimer) {
    clearInterval(spinnerTimer);
    spinnerTimer = null;
  }
  runInFlight = false;

  if (restore) {
    if (connected) setStatus("REPL connected");
    else if (window.bridgeReady) setStatus("Bridge ready");
    else setStatus("Bridge not ready");
  } else {
    setStatus("");
  }
}

function onRunResponseArrived() {
  if (!runInFlight) return;
  stopRunSpinner(false);
}

function sendRunBuffer(text, label = "Executing") {
  if (!bridge) throw new Error("Bridge not ready yet (QWebChannel not initialized).");
  if (!connected) throw new Error("Not connected.");
  startRunSpinner(label);
  try { bridge.run_buffer(text); }
  catch (e) { stopRunSpinner(true); throw e; }
}

// -----------------------
// Monaco sizing
// -----------------------
function scheduleEditorLayout() {
  try { if (editor) editor.layout(); } catch {}
  setTimeout(() => { try { if (editor) editor.layout(); } catch {} }, 0);
  setTimeout(() => { try { if (editor) editor.layout(); } catch {} }, 50);
  setTimeout(() => { try { if (editor) editor.layout(); } catch {} }, 150);
}

// -----------------------
// Terminals: REPL + Logs tabs (xterm)
// -----------------------
let activeTermTab = "repl";
let replTerm = null, replFit = null;
let logTerm = null, logFit = null;

let rlBuffer = "";
let history = [];
let histIdx = -1;

// Buffer terminal output until both terms are opened
let __termReady = false;
const __pendingRepl = [];
const __pendingLog = [];

function setActiveTab(which) {
  activeTermTab = which;
  const replBtn = $("tabRepl");
  const logsBtn = $("tabLogs");
  const replView = $("replView");
  const logsView = $("logsView");

  if (which === "repl") {
    replBtn.classList.add("active");
    logsBtn.classList.remove("active");
    replView.classList.add("active");
    logsView.classList.remove("active");
  } else {
    logsBtn.classList.add("active");
    replBtn.classList.remove("active");
    logsView.classList.add("active");
    replView.classList.remove("active");
  }
  refitAll();
}

function refitAll() {
  try { if (replFit) replFit.fit(); } catch {}
  try { if (logFit) logFit.fit(); } catch {}
  scheduleEditorLayout();
}

function writeReplLine(s) {
  const line = String(s);
  if (!replTerm || !__termReady) { __pendingRepl.push(line); return; }
  replTerm.writeln(line);
}
function writeReplRaw(s) {
  const txt = String(s);
  if (!replTerm || !__termReady) { __pendingRepl.push(txt); return; }
  replTerm.write(txt);
}
function writeLogLine(s) {
  const line = String(s);
  if (!logTerm || !__termReady) { __pendingLog.push(line); return; }
  logTerm.writeln(line);
}

function replPrompt() { writeReplRaw("\r\n>>> "); }
function replClearLine() { writeReplRaw("\r\x1b[2K>>> " + rlBuffer); }
function replSetBuffer(s) { rlBuffer = s || ""; replClearLine(); }

function initTerminals() {
  const Terminal = window.Terminal;
  const FitAddonCtor =
    window.FitAddon?.FitAddon ||
    window.FitAddonFit?.FitAddon ||
    null;

  if (!Terminal) {
    setStatus("xterm missing");
    return;
  }

  replTerm = new Terminal({
    convertEol: true,
    cursorBlink: true,
    fontFamily: "Consolas, 'Cascadia Mono', Menlo, monospace",
    fontSize: 13,
    theme: { background: "#0b0e17", foreground: "#e6e6e6" },
    scrollback: 5000,
  });
  replFit = FitAddonCtor ? new FitAddonCtor() : null;
  if (replFit) replTerm.loadAddon(replFit);

  logTerm = new Terminal({
    convertEol: true,
    cursorBlink: false,
    disableStdin: true,
    fontFamily: "Consolas, 'Cascadia Mono', Menlo, monospace",
    fontSize: 12,
    theme: { background: "#0b0e17", foreground: "#b9bccb" },
    scrollback: 8000,
  });
  logFit = FitAddonCtor ? new FitAddonCtor() : null;
  if (logFit) logTerm.loadAddon(logFit);

  const replHost = $("replTerminal");
  const logHost = $("logTerminal");

  const tryOpen = (attempt = 0) => {
    const r = replHost.getBoundingClientRect();
    const l = logHost.getBoundingClientRect();
    if ((r.height < 40 || l.height < 40) && attempt < 20) {
      setTimeout(() => tryOpen(attempt + 1), 50);
      return;
    }

    replTerm.open(replHost);
    logTerm.open(logHost);

    // Now terminals are ready: flush any buffered output
    __termReady = true;
    for (const line of __pendingRepl.splice(0)) {
      try { replTerm.writeln(line); } catch {}
    }
    for (const line of __pendingLog.splice(0)) {
      try { logTerm.writeln(line); } catch {}
    }

    refitAll();

    writeReplLine("\x1b[38;5;75mMicroPython Studio REPL\x1b[0m");
    writeReplLine("Enter sends to run_buffer().");
    replPrompt();

    writeLogLine("\x1b[38;5;244mMicroPython Studio Logs\x1b[0m");

    replTerm.onData((data) => {
      const code = data.charCodeAt(0);

      if (data === "\r") {
        const line = rlBuffer;
        rlBuffer = "";
        histIdx = -1;
        writeReplRaw("\r\n");

        if (line.trim().length) {
          history.unshift(line);
          if (history.length > 100) history.pop();
        }

        if (!bridge) {
          writeReplLine("\x1b[38;5;203mBridge not ready.\x1b[0m");
          replPrompt();
          return;
        }
        if (!connected) {
          writeReplLine("\x1b[38;5;203mNot connected.\x1b[0m");
          replPrompt();
          return;
        }

        try { sendRunBuffer(line + "\n", "Executing"); }
        catch (e) { writeReplLine("\x1b[38;5;203mSend failed:\x1b[0m " + e.toString()); }

        replPrompt();
        return;
      }

      if (code === 0x7f) {
        if (rlBuffer.length > 0) {
          rlBuffer = rlBuffer.slice(0, -1);
          replClearLine();
        }
        return;
      }

      if (data === "\u000c") {
        replTerm.clear();
        writeReplLine("\x1b[38;5;75mMicroPython Studio REPL\x1b[0m");
        replPrompt();
        return;
      }

      if (data >= " " && data !== "\x1b") {
        rlBuffer += data;
        writeReplRaw(data);
      }
    });

    replTerm.onKey(({ domEvent }) => {
      if (domEvent.key === "ArrowUp") {
        if (!history.length) return;
        if (histIdx + 1 < history.length) histIdx++;
        replSetBuffer(history[histIdx] || "");
        domEvent.preventDefault();
      }
      if (domEvent.key === "ArrowDown") {
        if (!history.length) return;
        if (histIdx <= 0) {
          histIdx = -1;
          replSetBuffer("");
        } else {
          histIdx--;
          replSetBuffer(history[histIdx] || "");
        }
        domEvent.preventDefault();
      }
    });

    try {
      if ("ResizeObserver" in window) {
        const ro = new ResizeObserver(() => refitAll());
        ro.observe($("terminalPane"));
        ro.observe($("replView"));
        ro.observe($("logsView"));
      }
    } catch {}
  };

  tryOpen(0);
}

// -----------------------
// Manual splitter for terminal height
// -----------------------
function initManualSplit() {
  const mainPane = $("mainPane");
  const gutter = $("splitGutter");
  const terminalPane = $("terminalPane");
  const editorPane = $("editorPane");

  let dragging = false;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function setTerminalHeight(px) {
    terminalPane.style.flex = `0 0 ${px}px`;
    terminalPane.style.height = `${px}px`;
    editorPane.style.flex = "1 1 auto";
    refitAll();
  }

  gutter.addEventListener("mousedown", (e) => {
    dragging = true;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "row-resize";
    e.preventDefault();
  });

  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
  });

  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = mainPane.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const total = rect.height;
    const gutterH = gutter.getBoundingClientRect().height || 8;

    const minTerm = 160;
    const maxTerm = Math.max(minTerm, total - 200);
    const desiredTerm = clamp(total - (y + gutterH), minTerm, maxTerm);
    setTerminalHeight(desiredTerm);
  });

  setTimeout(() => {
    const rect = mainPane.getBoundingClientRect();
    const fallback = clamp(Math.floor(rect.height * 0.32), 200, 420);
    setTerminalHeight(fallback);
  }, 0);
}

// -----------------------
// Project tree rendering
// -----------------------
function normPath(p) {
  return String(p || "").replace(/\\/g, "/");
}

function basename(p) {
  const s = normPath(p);
  const parts = s.split("/");
  return parts[parts.length - 1] || s;
}

function setSelectedNode(node) {
  selectedNode = node || null;
  $("selectedNodeLabel").textContent = selectedNode ? normPath(selectedNode.path) : "(none selected)";
  renderTree();
}

function toggleExpanded(path) {
  const p = normPath(path);
  if (expanded.has(p)) expanded.delete(p);
  else expanded.add(p);
  renderTree();
}

function filterMatch(nodeName, filter) {
  if (!filter) return true;
  return nodeName.toLowerCase().includes(filter.toLowerCase());
}

function subtreeHasMatch(node, filter) {
  const name = (node.name || basename(node.path)).toLowerCase();
  const f = filter.toLowerCase();
  if (name.includes(f)) return true;
  if (node.type === "dir" && node.children) return node.children.some(ch => subtreeHasMatch(ch, filter));
  return false;
}

function renderTree() {
  const treeEl = $("tree");
  treeEl.innerHTML = "";

  const filter = $("treeFilter").value.trim();

  if (!projectRoot) {
    treeEl.innerHTML = `<div class="muted" style="padding:8px;">Set a project root to view files.</div>`;
    return;
  }

  if (!projectTree) {
    treeEl.innerHTML = `<div class="muted" style="padding:8px;">(no data)</div>`;
    return;
  }

  const rootPath = normPath(projectTree.path);
  if (!expanded.has(rootPath)) expanded.add(rootPath);

  const walk = (node, depth) => {
    const name = node.name || basename(node.path);
    const path = normPath(node.path);
    const type = node.type;

    let nodeVisible = filterMatch(name, filter);
    if (filter && type === "dir" && node.children) {
      const anyChild = node.children.some(ch => subtreeHasMatch(ch, filter));
      nodeVisible = nodeVisible || anyChild;
    }
    if (!nodeVisible) return;

    const row = document.createElement("div");
    row.className = "treeNode" + (selectedNode && normPath(selectedNode.path) === path ? " selected" : "");
    row.dataset.path = path;

    for (let i = 0; i < depth; i++) {
      const ind = document.createElement("div");
      ind.className = "treeIndent";
      row.appendChild(ind);
    }

    const twisty = document.createElement("div");
    twisty.className = "treeTwisty";
    if (type === "dir") {
      twisty.textContent = expanded.has(path) ? "▾" : "▸";
      twisty.onclick = (e) => { e.stopPropagation(); toggleExpanded(path); };
    } else {
      twisty.textContent = "";
    }
    row.appendChild(twisty);

    const icon = document.createElement("div");
    icon.className = "treeIcon";
    icon.textContent = (type === "dir") ? "📁" : "📄";
    row.appendChild(icon);

    const label = document.createElement("div");
    label.className = "treeName";
    label.textContent = name;
    row.appendChild(label);

    if (type === "dir") {
      const badge = document.createElement("div");
      badge.className = "treeBadge";
      badge.textContent = node.children ? String(node.children.length) : "0";
      row.appendChild(badge);
    }

    row.onclick = async () => {
      setSelectedNode(node);
      if (type === "dir") {
        toggleExpanded(path);
        return;
      }
      await openFile(path);
    };

    treeEl.appendChild(row);

    if (type === "dir" && expanded.has(path) && node.children) {
      const kids = [...node.children].sort((a, b) => {
        if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
        return (a.name || "").localeCompare(b.name || "");
      });
      for (const ch of kids) walk(ch, depth + 1);
    }
  };

  walk(projectTree, 0);
}

// -----------------------
// File operations via bridge
// -----------------------
async function refreshTree() {
  if (!bridge) { writeLogLine("[js] bridge not ready"); return; }
  if (!projectRoot) { renderTree(); return; }

  try {
    const data = await bridge.fs_tree(projectRoot);
    projectTree = data;
    writeLogLine("[js] tree refreshed");
    renderTree();
  } catch (e) {
    writeLogLine("[js] fs_tree failed: " + e.toString());
  }
}

async function openFile(path) {
  if (!bridge) return;
  try {
    const content = await bridge.fs_read(path);
    setActiveFileLabel(path);
    if (editor) editor.setValue(content || "");
    scheduleEditorLayout();
    setStatus("");

    // ---- Pyright LSP: open the document in the server
    lspOpenActiveDocument();

  } catch (e) {
    writeLogLine("[js] fs_read failed: " + e.toString());
  }
}

async function saveActiveFile() {
  if (!bridge) return alert("Bridge not ready.");
  if (!activeFilePath) return alert("No active file to save.");
  try {
    await bridge.fs_write(activeFilePath, editor ? editor.getValue() : "");
    writeLogLine("[js] saved: " + normPath(activeFilePath));
    setStatus("");

    // optional: push latest content to LSP quickly
    lspDidChangeDebounced(true);

  } catch (e) {
    writeLogLine("[js] fs_write failed: " + e.toString());
    alert("Save failed (see Logs).");
  }
}

function pickTargetDirForCreate() {
  if (selectedNode && selectedNode.type === "dir") return normPath(selectedNode.path);
  if (selectedNode && selectedNode.type === "file") {
    const p = normPath(selectedNode.path);
    return p.substring(0, p.lastIndexOf("/")) || projectRoot;
  }
  return projectRoot;
}

async function createFile() {
  if (!bridge) return alert("Bridge not ready.");
  if (!projectRoot) return alert("Set project root first.");
  const dir = pickTargetDirForCreate();
  const name = prompt("New file name (e.g. main.py):", "new_file.py");
  if (!name) return;

  const full = normPath(dir) + "/" + name;
  try {
    await bridge.fs_write(full, "# " + name + "\n");
    await refreshTree();
    await openFile(full);
  } catch (e) {
    writeLogLine("[js] create file failed: " + e.toString());
  }
}

async function createFolder() {
  if (!bridge) return alert("Bridge not ready.");
  if (!projectRoot) return alert("Set project root first.");
  const dir = pickTargetDirForCreate();
  const name = prompt("New folder name:", "folder");
  if (!name) return;

  const full = normPath(dir) + "/" + name;
  try {
    await bridge.fs_mkdir(full);
    await refreshTree();
  } catch (e) {
    writeLogLine("[js] mkdir failed: " + e.toString());
  }
}

async function deleteSelected() {
  if (!bridge) return alert("Bridge not ready.");
  if (!selectedNode) return alert("Select a file or folder first.");
  const p = normPath(selectedNode.path);
  if (!confirm("Delete?\n" + p)) return;

  try {
    await bridge.fs_delete(p);
    if (activeFilePath && normPath(activeFilePath) === p) {
      // Close LSP doc too
      lspCloseActiveDocument();
      setActiveFileLabel("");
      if (editor) editor.setValue("");
    }
    selectedNode = null;
    $("selectedNodeLabel").textContent = "(none selected)";
    await refreshTree();
  } catch (e) {
    writeLogLine("[js] delete failed: " + e.toString());
  }
}

async function newProject() {
  if (!bridge) return alert("Bridge not ready.");
  const root = $("projectRoot").value.trim();
  if (!root) return alert("Set a project root path first.");
  const tpl = $("projectTemplate").value;

  try {
    await bridge.project_generate(root, tpl);
    projectRoot = root;
    $("projectRoot").value = projectRoot;
    expanded.clear();
    await refreshTree();
    writeLogLine("[js] project generated: " + projectRoot);
  } catch (e) {
    writeLogLine("[js] project_generate failed: " + e.toString());
    alert("Project generate failed (see Logs).");
  }
}

// -----------------------
// Device/log routing
// -----------------------
function looksLikeLogLine(s) {
  return (
    s.startsWith("[scan]") ||
    s.startsWith("[run]") ||
    s.startsWith("[js]") ||
    s.startsWith("[flash]") ||
    s.startsWith("[repl]") ||
    s.startsWith("replStatus:") ||
    s.startsWith("[bridge]") ||
    s.startsWith("[web]")
  );
}
function looksLikeDeviceOut(s) {
  return (
    s.startsWith("OK") ||
    s.includes("Traceback") ||
    s.includes("SyntaxError") ||
    s.includes("NameError") ||
    s === ">" ||
    s.startsWith("> ")
  );
}

function writeLog(s) { writeLogLine("[py] " + s); }

function writeDevice(s) {
  let out = String(s);
  if (out.startsWith("OK") && out.length > 2) {
    const rest = out.slice(2);
    if (rest[0] !== "\n" && rest[0] !== "\r" && rest[0] !== " ") out = "OK\n" + rest;
  }
  if (runInFlight && (looksLikeDeviceOut(out) || (!looksLikeLogLine(out) && !out.startsWith("[") && out.trim() !== ""))) {
    onRunResponseArrived();
  }
  for (const line of out.replace(/\r\n/g, "\n").split("\n")) writeReplLine(line);
}

// -----------------------
// UI wiring
// -----------------------
function updateDeviceDropdown(devices) {
  const sel = $("deviceSelect");
  sel.innerHTML = "";

  if (!devices || !devices.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(no devices found)";
    sel.appendChild(opt);
    selectedPort = "";
    return;
  }

  for (const d of devices) {
    const opt = document.createElement("option");
    opt.value = d.port || "";
    opt.textContent = d.label || d.port || "(device)";
    sel.appendChild(opt);
  }

  const stillThere = devices.some(d => d.port === selectedPort);
  if (!stillThere) selectedPort = devices[0].port || "";
  sel.value = selectedPort;
}

function wireButtons() {
  $("btnScan").onclick = () => {
    if (!bridge) return alert("Bridge not ready.");
    bridge.scan_devices();
  };

  $("btnConnect").onclick = () => {
    if (!bridge) return alert("Bridge not ready.");
    if (!selectedPort) return alert("No device selected.");
    bridge.connect_repl(selectedPort, 115200);
  };

  $("btnDisconnect").onclick = () => {
    if (!bridge) return alert("Bridge not ready.");
    bridge.disconnect_repl();
  };

  $("btnPush").onclick = () => {
    if (!bridge) return alert("Bridge not ready.");
    if (!connected) return alert("Connect REPL first.");
    const remotePath = $("pathRemote").value.trim() || "main.py";
    bridge.push_file(remotePath, editor ? editor.getValue() : "");
  };

  $("btnRun").onclick = () => {
    if (!bridge) return alert("Bridge not ready.");
    if (!connected) return alert("Connect REPL first.");
    try { sendRunBuffer(editor ? editor.getValue() : "", "Executing"); }
    catch (e) { alert(e.toString()); }
  };

  $("btnSaveFile").onclick = () => saveActiveFile();

  $("deviceSelect").onchange = (e) => { selectedPort = e.target.value || ""; };

  $("tabRepl").onclick = () => setActiveTab("repl");
  $("tabLogs").onclick = () => setActiveTab("logs");
  $("btnClearLogs").onclick = () => {
    if (logTerm) logTerm.clear();
    writeLogLine("\x1b[38;5;244mLogs cleared.\x1b[0m");
  };

  $("btnRefreshTree").onclick = () => refreshTree();
  $("btnSetRoot").onclick = async () => {
    const root = $("projectRoot").value.trim();
    if (!root) return alert("Enter a project root path.");
    projectRoot = root;
    expanded.clear();
    await refreshTree();
  };

  $("btnNewProject").onclick = () => newProject();
  $("btnNewFile").onclick = () => createFile();
  $("btnNewFolder").onclick = () => createFolder();
  $("btnDeleteNode").onclick = () => deleteSelected();

  $("treeFilter").oninput = () => renderTree();

  // ---- Flasher UI (moved into WebView) ----
  try {
    $("btnBrowseFw").onclick = async () => {
      if (!bridge) return alert("Bridge not ready.");
      if (!bridge.ui_pick_firmware) {
        return alert("Bridge missing ui_pick_firmware(). Update main.py.");
      }
      try {
        const p = await bridge.ui_pick_firmware();
        if (p) $("flashFwPath").value = String(p);
      } catch (e) {
        alert("Browse failed: " + e.toString());
      }
    };

    $("btnFlashFirmware").onclick = () => {
      if (!bridge) return alert("Bridge not ready.");
      if (!bridge.flash_firmware) {
        return alert("Bridge missing flash_firmware(). Update main.py.");
      }
      if (!selectedPort) return alert("No device selected.");

      const fw = $("flashFwPath").value.trim();
      const baud = parseInt($("flashBaud").value, 10) || 460800;
      const addr = $("flashAddr").value.trim() || "0x1000";
      const eraseFirst = $("flashEraseFirst").checked;
      if (!fw) return alert("Select a firmware .bin path.");

      flashSetControlsEnabled(false);
      flashSetProgress("indeterminate", "Starting…", 0);
      try {
        bridge.flash_firmware(String(selectedPort), String(fw), Number(baud), String(addr), !!eraseFirst);
      } catch (e) {
        flashSetControlsEnabled(true);
        flashSetProgress("error", "Error", 0);
        alert(e.toString());
      }
    };

    $("btnEraseFlash").onclick = () => {
      if (!bridge) return alert("Bridge not ready.");
      if (!bridge.flash_erase) {
        return alert("Bridge missing flash_erase(). Update main.py.");
      }
      if (!selectedPort) return alert("No device selected.");

      const baud = parseInt($("flashBaud").value, 10) || 460800;
      flashSetControlsEnabled(false);
      flashSetProgress("indeterminate", "Erasing…", 0);
      try {
        bridge.flash_erase(String(selectedPort), Number(baud));
      } catch (e) {
        flashSetControlsEnabled(true);
        flashSetProgress("error", "Error", 0);
        alert(e.toString());
      }
    };
  } catch {}

  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveActiveFile();
    }
  });
}

// -----------------------
// Monaco init (guarded)
// -----------------------
let monacoReady = false;

function initMonaco() {
  // If Monaco already loaded, only create editor once.
  if (window.monaco && window.monaco.editor) {
    if (editor) {
      monacoReady = true;
      writeLogLine("[js] Monaco already loaded; editor exists.");
      return;
    }
    try {
      editor = window.monaco.editor.create($("editor"), {
        value: "# Multi-file projects enabled\nprint('hello')\n",
        language: "python",
        theme: "vs-dark",
        automaticLayout: true,
        minimap: { enabled: false },
        fontSize: 14,
      });
      monacoReady = true;
      writeLogLine("[js] Monaco reused (already loaded).");
      scheduleEditorLayout();

      editor.onDidChangeModelContent(() => {
        lspDidChangeDebounced(false);
      });
    } catch (e) {
      writeLogLine("[js] Monaco reuse failed: " + e.toString());
    }
    return;
  }

  // Prevent double bootstraps which cause "Duplicate definition of module 'vs/editor/editor.main'"
  if (window.__monacoBootstrapping) {
    writeLogLine("[js] Monaco bootstrap already in progress; skipping duplicate init.");
    return;
  }
  window.__monacoBootstrapping = true;

  try {
    if (!window.require || !window.require.config) {
      writeLogLine("[js] requirejs missing; Monaco loader not available.");
      window.__monacoBootstrapping = false;
      return;
    }

    // Configure once
    if (!window.__monacoPathsConfigured) {
      window.require.config({
        paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/vs" }
      });
      window.__monacoPathsConfigured = true;
    }

    window.require(["vs/editor/editor.main"], () => {
      if (editor) {
        monacoReady = true;
        writeLogLine("[js] Monaco loaded; editor already exists.");
        window.__monacoBootstrapping = false;
        scheduleEditorLayout();
        return;
      }

      editor = window.monaco.editor.create($("editor"), {
        value: "# Multi-file projects enabled\nprint('hello')\n",
        language: "python",
        theme: "vs-dark",
        automaticLayout: true,
        minimap: { enabled: false },
        fontSize: 14,
      });

      monacoReady = true;
      writeLogLine("[js] Monaco initialized.");
      window.__monacoBootstrapping = false;

      scheduleEditorLayout();

      editor.onDidChangeModelContent(() => {
        lspDidChangeDebounced(false);
      });

    }, (err) => {
      window.__monacoBootstrapping = false;
      writeLogLine("[js] Monaco require() error: " + JSON.stringify(err));
    });
  } catch (e) {
    window.__monacoBootstrapping = false;
    writeLogLine("[js] initMonaco exception: " + e.toString());
  }
}

// -----------------------
// Pyright LSP glue (expects Bridge LSP methods + diagnostics signal)
// -----------------------
let lspEnabled = true;
let lspStarted = false;
let lspActiveUri = "";
let lspChangeTimer = null;
const LSP_DEBOUNCE_MS = 350;

function filePathToUri(path) {
  const p = normPath(path);
  if (p.startsWith("file://")) return p;
  if (/^[A-Za-z]:\//.test(p)) return "file:///" + p;
  if (p.startsWith("/")) return "file://" + p;
  return "file:///" + p;
}

function monacoClearMarkers() {
  try {
    if (!editor || !monacoReady) return;
    const model = editor.getModel();
    if (!model) return;
    monaco.editor.setModelMarkers(model, "pyright", []);
  } catch {}
}

function lspEnsureStarted() {
  if (!lspEnabled) return;
  if (!bridge) return;
  if (lspStarted) return;

  try {
    if (bridge.lsp_start) {
      bridge.lsp_start();
      writeLogLine("[js] lsp_start called.");
    }
  } catch (e) {
    writeLogLine("[js] lsp_start failed: " + e.toString());
  }

  lspStarted = true;
}

function lspCloseActiveDocument() {
  if (!lspEnabled) return;
  if (!bridge) return;
  if (!lspActiveUri) return;

  try {
    if (bridge.lsp_close) {
      bridge.lsp_close(lspActiveUri);
      writeLogLine("[js] LSP close: " + lspActiveUri);
    }
  } catch (e) {
    writeLogLine("[js] lsp_close failed: " + e.toString());
  }

  lspActiveUri = "";
  monacoClearMarkers();
}

function lspOpenActiveDocument() {
  if (!lspEnabled) return;
  if (!bridge || !editor || !monacoReady) return;
  if (!activeFilePath) return;

  lspEnsureStarted();

  const uri = filePathToUri(activeFilePath);
  const text = editor.getValue() || "";

  if (lspActiveUri && lspActiveUri !== uri) {
    lspCloseActiveDocument();
  }

  if (!bridge.lsp_open) {
    writeLogLine("[js] pyright LSP not available: bridge.lsp_open missing.");
    return;
  }

  try {
    bridge.lsp_open(uri, text);
    lspActiveUri = uri;
    writeLogLine("[js] LSP open: " + uri);
  } catch (e) {
    writeLogLine("[js] lsp_open failed: " + e.toString());
  }
}

function lspDidChangeDebounced(immediate) {
  if (!lspEnabled) return;
  if (!bridge || !editor || !monacoReady) return;
  if (!activeFilePath) return;

  if (!lspActiveUri) {
    lspOpenActiveDocument();
  }

  if (!bridge.lsp_change) return;

  const send = () => {
    if (!lspActiveUri) return;
    try {
      bridge.lsp_change(lspActiveUri, editor.getValue() || "");
    } catch (e) {
      writeLogLine("[js] lsp_change failed: " + e.toString());
    }
  };

  if (immediate) {
    if (lspChangeTimer) clearTimeout(lspChangeTimer);
    lspChangeTimer = null;
    send();
    return;
  }

  if (lspChangeTimer) clearTimeout(lspChangeTimer);
  lspChangeTimer = setTimeout(() => {
    lspChangeTimer = null;
    send();
  }, LSP_DEBOUNCE_MS);
}

function lspApplyDiagnostics(uri, diags) {
  if (!editor || !monacoReady) return;
  if (!lspActiveUri || uri !== lspActiveUri) return;

  const model = editor.getModel();
  if (!model) return;

  const markers = [];
  const list = Array.isArray(diags) ? diags : [];

  const mapSeverity = (sev) => {
    if (sev === 1) return 8;
    if (sev === 2) return 4;
    if (sev === 3) return 2;
    if (sev === 4) return 1;
    return 4;
  };

  for (const d of list) {
    const r = d.range || {};
    const s = r.start || {};
    const e = r.end || {};

    const startLineNumber = (s.line ?? 0) + 1;
    const startColumn = (s.character ?? 0) + 1;
    const endLineNumber = (e.line ?? (s.line ?? 0)) + 1;
    const endColumn = (e.character ?? ((s.character ?? 0) + 1)) + 1;

    const msg = String(d.message || "Diagnostic");
    const sev = mapSeverity(d.severity);

    markers.push({
      startLineNumber,
      startColumn,
      endLineNumber,
      endColumn,
      message: msg,
      severity: sev,
      source: "pyright",
    });
  }

  try {
    monaco.editor.setModelMarkers(model, "pyright", markers);
  } catch (e) {
    writeLogLine("[js] setModelMarkers failed: " + e.toString());
  }
}

// -----------------------
// WebChannel
// -----------------------
function setBridgeReady(ready) {
  window.bridgeReady = !!ready;
  if (!runInFlight) setStatus(ready ? "Bridge ready" : "Bridge not ready");
}

function attachBridgeSignals() {
  try {
    if (bridge.logMessage && bridge.logMessage.connect) {
      bridge.logMessage.connect((msg) => {
        const s = String(msg);
        if (s.startsWith("[run] executing")) return; // suppress
        if (looksLikeLogLine(s)) writeLog(s);
        else writeDevice(s);
      });
    }

    if (bridge.deviceList && bridge.deviceList.connect) {
      bridge.deviceList.connect((devices) => {
        lastDevices = devices || [];
        updateDeviceDropdown(lastDevices);
        writeLogLine("[js] deviceList updated, count=" + lastDevices.length);
      });
    }

    if (bridge.replStatus && bridge.replStatus.connect) {
      bridge.replStatus.connect((isConnected, msg) => {
        connected = !!isConnected;
        if (!runInFlight) setStatus(connected ? "REPL connected" : "Bridge ready");
        writeLogLine("[py] replStatus: " + msg);
      });
    }

    if (bridge.lspDiagnostics && bridge.lspDiagnostics.connect) {
      bridge.lspDiagnostics.connect((uri, diagnostics) => {
        try {
          const u = String(uri || "");
          const diags = diagnostics || [];
          lspApplyDiagnostics(u, diags);
        } catch (e) {
          writeLogLine("[js] lspDiagnostics handler error: " + e.toString());
        }
      });
      writeLogLine("[js] LSP diagnostics signal attached.");
    } else {
      writeLogLine("[js] LSP diagnostics signal not available (bridge.lspDiagnostics missing).");
    }

    if (bridge.flashStatus && bridge.flashStatus.connect) {
      bridge.flashStatus.connect((mode, text, percent) => {
        try {
          const m = String(mode || "idle");
          const t = String(text || "");
          const p = (percent === undefined || percent === null) ? 0 : Number(percent);
          flashSetProgress(m, t, p);

          if (m === "idle") {
            flashSetControlsEnabled(true);
          } else if (m === "done" || m === "error") {
            setTimeout(() => flashSetControlsEnabled(true), 300);
          }
        } catch (e) {
          writeLogLine("[js] flashStatus handler error: " + e.toString());
        }
      });
      writeLogLine("[js] flashStatus signal attached.");
    } else {
      writeLogLine("[js] flashStatus signal not available (bridge.flashStatus missing).");
    }

  } catch (e) {
    writeLogLine("[js] attachBridgeSignals error: " + e.toString());
  }
}

function initWebChannel() {
  if (typeof QWebChannel === "undefined") {
    writeLogLine("[js] QWebChannel undefined.");
    setBridgeReady(false);
    return;
  }
  if (!window.qt || !qt.webChannelTransport) {
    writeLogLine("[js] qt.webChannelTransport missing.");
    setBridgeReady(false);
    return;
  }

  try {
    new QWebChannel(qt.webChannelTransport, (channel) => {
      bridge = channel.objects.bridge;
      if (!bridge) {
        writeLogLine("[js] bridge object not found.");
        setBridgeReady(false);
        return;
      }

      setBridgeReady(true);
      writeLogLine("[js] Bridge ready.");
      attachBridgeSignals();

      // JS -> Py proof (optional; harmless if missing)
      try { if (bridge.js_log) bridge.js_log("JS: WebChannel attached"); } catch {}

      // Py -> JS proof (requires Bridge.ping() slot you added)
      try {
        if (bridge.ping) {
          Promise.resolve(bridge.ping())
            .then((r) => writeLogLine("[js] ping result: " + r))
            .catch((e) => writeLogLine("[js] ping failed: " + e));
        } else {
          writeLogLine("[js] bridge.ping missing (add ping() slot in main.py).");
        }
      } catch (e) {
        writeLogLine("[js] ping exception: " + e);
      }

      // Start LSP early (optional)
      try {
        if (bridge.lsp_start) {
          bridge.lsp_start();
          lspStarted = true;
          writeLogLine("[js] lsp_start invoked.");
        }
      } catch (e) {
        writeLogLine("[js] lsp_start failed: " + e.toString());
      }

      try { bridge.scan_devices(); writeLogLine("[js] scan_devices invoked."); }
      catch (e) { writeLogLine("[js] scan_devices call failed: " + e.toString()); }

      // Default project root
      try {
        if (bridge.fs_get_cwd) {
          bridge.fs_get_cwd().then((cwd) => {
            if (!projectRoot) {
              projectRoot = String(cwd || "");
              $("projectRoot").value = projectRoot;
              refreshTree();
            }
          });
        }
      } catch {}
    });
  } catch (e) {
    writeLogLine("[js] WebChannel init exception: " + e.toString());
    setBridgeReady(false);
  }
}

// -----------------------
// Boot
// -----------------------
window.onload = () => {
  window.bridgeReady = false;
  setStatus("Loading…");
  setActiveFileLabel("");

  wireButtons();
  setActiveTab("repl");

  // Flasher UI initial state
  try {
    flashSetControlsEnabled(true);
    flashSetProgress("idle", "Idle", 0);
  } catch {}

  // Order: terminals early so logs are visible even if Monaco has issues
  initTerminals();
  initManualSplit();
  initWebChannel();

  // Monaco last (guarded)
  initMonaco();

  setTimeout(() => writeLogLine("[ui] ready"), 250);
};