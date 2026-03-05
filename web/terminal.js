// web/terminal.js
// Self-contained terminal subsystem for MicroPython Studio.
// Depends on xterm + fit addon being loaded globally.
// Provides buffering until mounted and a simple REPL line input callback.

(function () {
    function safeGet(id) { return document.getElementById(id); }
    function mustGet(id) {
      const el = document.getElementById(id);
      if (!el) throw new Error(`terminal.js: Missing element #${id}`);
      return el;
    }
  
    const DEFAULT_THEME_REPL = { background: "#0b0e17", foreground: "#e6e6e6" };
    const DEFAULT_THEME_LOG  = { background: "#0b0e17", foreground: "#b9bccb" };
  
    function pickFitAddon() {
      return (
        window.FitAddon?.FitAddon ||
        window.FitAddonFit?.FitAddon ||
        null
      );
    }
  
    class TerminalUI {
      constructor(opts) {
        this.ids = opts.ids;
        this.onLine = null;
  
        this.replTerm = null;
        this.logTerm = null;
        this.replFit = null;
        this.logFit = null;
  
        this._ready = false;
        this._pendingRepl = [];
        this._pendingLog = [];
  
        this._rl = "";
        this._history = [];
        this._histIdx = -1;
  
        this._resizeObserver = null;
      }
  
      // -------- public API --------
      setActiveTab(which) {
        const { tabLog, tabRepl, wrapLog, wrapRepl } = this.ids;
        const tLog = mustGet(tabLog);
        const tRepl = mustGet(tabRepl);
        const wLog = mustGet(wrapLog);
        const wRepl = mustGet(wrapRepl);
  
        if (which === "repl") {
          tRepl.classList.add("active");
          tLog.classList.remove("active");
          wRepl.classList.add("active");
          wLog.classList.remove("active");
        } else {
          tLog.classList.add("active");
          tRepl.classList.remove("active");
          wLog.classList.add("active");
          wRepl.classList.remove("active");
        }
        this.fit();
      }
  
      fit() {
        try { if (this.replFit) this.replFit.fit(); } catch {}
        try { if (this.logFit) this.logFit.fit(); } catch {}
      }
  
      logLine(s) {
        const line = String(s);
        if (!this._ready || !this.logTerm) { this._pendingLog.push(line); return; }
        this.logTerm.writeln(line);
      }
  
      replLine(s) {
        const line = String(s);
        if (!this._ready || !this.replTerm) { this._pendingRepl.push(line); return; }
        this.replTerm.writeln(line);
      }
  
      replWrite(s) {
        const txt = String(s);
        if (!this._ready || !this.replTerm) { this._pendingRepl.push(txt); return; }
        this.replTerm.write(txt);
      }
  
      clearLogs() {
        try { if (this.logTerm) this.logTerm.clear(); } catch {}
      }
  
      onReplLine(cb) {
        this.onLine = cb;
      }
  
      mount() {
        const Terminal = window.Terminal;
        if (!Terminal) throw new Error("terminal.js: xterm Terminal not found (did you include xterm.js?)");
  
        const FitCtor = pickFitAddon();
  
        const {
          mountLog, mountRepl,
          tabLog, tabRepl,
        } = this.ids;
  
        // Wire tabs immediately (so you can click even if mount is delayed)
        mustGet(tabLog).onclick = () => this.setActiveTab("log");
        mustGet(tabRepl).onclick = () => this.setActiveTab("repl");
  
        this.replTerm = new Terminal({
          convertEol: true,
          cursorBlink: true,
          fontFamily: "Consolas, 'Cascadia Mono', Menlo, monospace",
          fontSize: 13,
          theme: DEFAULT_THEME_REPL,
          scrollback: 5000,
        });
        this.logTerm = new Terminal({
          convertEol: true,
          cursorBlink: false,
          disableStdin: true,
          fontFamily: "Consolas, 'Cascadia Mono', Menlo, monospace",
          fontSize: 12,
          theme: DEFAULT_THEME_LOG,
          scrollback: 8000,
        });
  
        this.replFit = FitCtor ? new FitCtor() : null;
        this.logFit = FitCtor ? new FitCtor() : null;
        if (this.replFit) this.replTerm.loadAddon(this.replFit);
        if (this.logFit) this.logTerm.loadAddon(this.logFit);
  
        const replHost = mustGet(mountRepl);
        const logHost = mustGet(mountLog);
  
        const tryOpen = (attempt = 0) => {
          const r = replHost.getBoundingClientRect();
          const l = logHost.getBoundingClientRect();
          if ((r.height < 40 || l.height < 40) && attempt < 30) {
            setTimeout(() => tryOpen(attempt + 1), 50);
            return;
          }
  
          this.replTerm.open(replHost);
          this.logTerm.open(logHost);
  
          this._ready = true;
  
          // Flush pending
          for (const line of this._pendingLog.splice(0)) this.logTerm.writeln(line);
          for (const line of this._pendingRepl.splice(0)) this.replTerm.writeln(line);
  
          this.fit();
  
          // default banners
          this.replLine("\x1b[38;5;75mMicroPython Studio REPL\x1b[0m");
          this.replLine("Enter sends to run_buffer().");
          this._prompt();
          this.logLine("\x1b[38;5;244mMicroPython Studio Logs\x1b[0m");
  
          this._wireReplInput();
          this._wireResizeObserver();
        };
  
        tryOpen(0);
        this.setActiveTab("log");
      }
  
      // -------- internals --------
      _prompt() { this.replWrite("\r\n>>> "); }
      _clearLine() { this.replWrite("\r\x1b[2K>>> " + this._rl); }
      _setBuffer(s) { this._rl = s || ""; this._clearLine(); }
  
      _wireReplInput() {
        this.replTerm.onData((data) => {
          const code = data.charCodeAt(0);
  
          if (data === "\r") {
            const line = this._rl;
            this._rl = "";
            this._histIdx = -1;
            this.replWrite("\r\n");
  
            if (line.trim().length) {
              this._history.unshift(line);
              if (this._history.length > 100) this._history.pop();
            }
  
            if (typeof this.onLine === "function") {
              try { this.onLine(line); } catch (e) { this.logLine("[terminal] onLine error: " + e); }
            } else {
              this.replLine("\x1b[38;5;203mNo REPL handler attached.\x1b[0m");
            }
  
            this._prompt();
            return;
          }
  
          if (code === 0x7f) { // backspace
            if (this._rl.length > 0) {
              this._rl = this._rl.slice(0, -1);
              this._clearLine();
            }
            return;
          }
  
          if (data === "\u000c") { // ctrl-l
            try { this.replTerm.clear(); } catch {}
            this.replLine("\x1b[38;5;75mMicroPython Studio REPL\x1b[0m");
            this._prompt();
            return;
          }
  
          if (data >= " " && data !== "\x1b") {
            this._rl += data;
            this.replWrite(data);
          }
        });
  
        this.replTerm.onKey(({ domEvent }) => {
          if (domEvent.key === "ArrowUp") {
            if (!this._history.length) return;
            if (this._histIdx + 1 < this._history.length) this._histIdx++;
            this._setBuffer(this._history[this._histIdx] || "");
            domEvent.preventDefault();
          }
          if (domEvent.key === "ArrowDown") {
            if (!this._history.length) return;
            if (this._histIdx <= 0) {
              this._histIdx = -1;
              this._setBuffer("");
            } else {
              this._histIdx--;
              this._setBuffer(this._history[this._histIdx] || "");
            }
            domEvent.preventDefault();
          }
        });
      }
  
      _wireResizeObserver() {
        try {
          if (!("ResizeObserver" in window)) return;
          const obsTargets = [];
          const tp = safeGet(this.ids.terminalPane);
          if (tp) obsTargets.push(tp);
          obsTargets.push(mustGet(this.ids.wrapLog));
          obsTargets.push(mustGet(this.ids.wrapRepl));
  
          this._resizeObserver = new ResizeObserver(() => this.fit());
          for (const t of obsTargets) this._resizeObserver.observe(t);
        } catch {}
      }
    }
  
    // Factory
    window.TerminalUI = {
      create(opts) {
        const ui = new TerminalUI(opts);
        return ui;
      }
    };
  })();