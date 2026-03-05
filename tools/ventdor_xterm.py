from __future__ import annotations

import shutil
from pathlib import Path
import sys


def die(msg: str, code: int = 1) -> None:
    print(f"[vendor_xterm] {msg}", file=sys.stderr)
    raise SystemExit(code)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[vendor_xterm] copied: {src} -> {dst}")


def find_in_node_modules(repo_root: Path) -> dict[str, Path] | None:
    """
    Try common layouts:
      node_modules/xterm/css/xterm.css
      node_modules/xterm/lib/xterm.js

      node_modules/xterm-addon-fit/lib/xterm-addon-fit.js
    """
    nm = repo_root / "node_modules"
    if not nm.exists():
        return None

    candidates = {
        "xterm_js": nm / "xterm" / "lib" / "xterm.js",
        "xterm_css": nm / "xterm" / "css" / "xterm.css",
        "fit_js": nm / "xterm-addon-fit" / "lib" / "xterm-addon-fit.js",
    }

    if all(p.exists() for p in candidates.values()):
        return candidates

    # Some installs may store xterm.js under dist/
    alt = {
        "xterm_js": nm / "xterm" / "dist" / "xterm.js",
        "xterm_css": nm / "xterm" / "dist" / "xterm.css",
        "fit_js": nm / "xterm-addon-fit" / "dist" / "xterm-addon-fit.js",
    }
    if all(p.exists() for p in alt.values()):
        return alt

    # If node_modules exists but paths aren't present, return None (we'll give guidance)
    return None


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = repo_root / "web" / "vendor" / "xterm"

    found = find_in_node_modules(repo_root)
    if not found:
        die(
            "Could not find xterm files in node_modules.\n"
            "Fix:\n"
            "  npm install xterm xterm-addon-fit\n"
            "Then re-run:\n"
            "  python tools/vendor_xterm.py\n"
        )

    out_map = {
        "xterm.js": found["xterm_js"],
        "xterm.css": found["xterm_css"],
        "xterm-addon-fit.js": found["fit_js"],
    }

    print(f"[vendor_xterm] output dir: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy with exact target names we expect in editor.html
    copy_file(out_map["xterm.js"], out_dir / "xterm.js")
    copy_file(out_map["xterm.css"], out_dir / "xterm.css")
    copy_file(out_map["xterm-addon-fit.js"], out_dir / "xterm-addon-fit.js")

    print("[vendor_xterm] done.")


if __name__ == "__main__":
    main()