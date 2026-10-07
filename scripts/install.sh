#!/bin/bash
# Install a `herdr-py` command that runs this checkout. No network access needed.
# usage: scripts/install.sh [BIN_DIR]   (default ~/.local/bin)
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
bin="${1:-$HOME/.local/bin}"
py="$(command -v python3 || true)"
[ -z "$py" ] && [ -x /usr/libexec/platform-python ] && py=/usr/libexec/platform-python   # RHEL 8 without python3
[ -z "$py" ] && { echo "no python3 found" >&2; exit 1; }
"$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' || { echo "herdr-py needs Python 3.6+" >&2; exit 1; }
mkdir -p "$bin"
cat > "$bin/herdr-py" <<WRAP
#!/bin/sh
PYTHONPATH="$repo\${PYTHONPATH:+:\$PYTHONPATH}" exec "$py" -m herdr_py "\$@"
WRAP
chmod 755 "$bin/herdr-py"
echo "installed $bin/herdr-py (python: $py)"
case ":$PATH:" in *":$bin:"*) ;; *) echo "note: $bin is not in PATH" ;; esac
