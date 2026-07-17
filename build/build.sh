#!/usr/bin/env bash
#
# Baut "iCloud Sync.app" mit py2app und signiert sie.
# Vom Repo-Root ausführen:  bash build/build.sh
#
# Signatur-Identität via CODESIGN_IDENTITY (Default "-" = ad-hoc). Mit einer STABILEN
# (z. B. self-signed) Identität bleibt die Schlüsselbund-Freigabe über Updates erhalten —
# sonst fragt macOS nach jedem Build erneut. Beispiel:
#   CODESIGN_IDENTITY="iCloud Sync Selfsign" bash build/build.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-.venv/bin/python}"
APP="dist/iCloud Sync.app"
SIGN_ID="${CODESIGN_IDENTITY:--}"   # "-" = ad-hoc; sonst stabile (self-signed) Identität

if [[ ! -x "$PY" ]]; then
  echo "Kein venv-Python unter $PY. Erst: /opt/homebrew/bin/python3.13 -m venv .venv" >&2
  exit 1
fi

# Läuft die App aus genau diesem dist/, würde der Build ihr das Bundle unter den Füßen
# weglöschen: der Prozess läuft danach aus einem gelöschten Bundle mit ALTEM Code weiter,
# der Fix wirkt also nicht. Aus /Applications gestartete Instanzen sind unkritisch.
RUNNING="$(pgrep -f "$ROOT/dist/iCloud Sync.app/Contents/MacOS/iCloud Sync" || true)"
if [[ -n "$RUNNING" ]]; then
  echo "ABBRUCH: 'iCloud Sync' läuft gerade aus $ROOT/dist (PID: ${RUNNING//$'\n'/ })." >&2
  echo "         Der Build würde das laufende Bundle löschen." >&2
  echo "         Erst die App beenden (Menüleiste -> Beenden), dann erneut bauen." >&2
  exit 1
fi

echo "==> Build-Abhängigkeiten"
"$PY" -m pip install --quiet -r requirements-build.txt

echo "==> py2app-Build"
# Finder/Spotlight legt .DS_Store gern mitten im Löschen neu an -> "Directory not empty".
# Ein paar Mal nachfassen, statt am Race zu scheitern.
rm -rf build/_py2app
for _ in 1 2 3; do
  rm -rf dist 2>/dev/null || true
  [[ -e dist ]] || break
  sleep 1
done
if [[ -e dist ]]; then
  echo "ABBRUCH: dist/ ließ sich nicht löschen (Inhalt: $(ls -A dist | tr '\n' ' '))." >&2
  exit 1
fi
"$PY" build/setup.py py2app --dist-dir dist --bdist-base build/_py2app

if [[ "$SIGN_ID" == "-" ]]; then
  echo "==> Signierung: ad-hoc (CODESIGN_IDENTITY nicht gesetzt)"
else
  echo "==> Signierung: $SIGN_ID"
fi
codesign --force --deep --sign "$SIGN_ID" "$APP"
codesign --verify --deep --strict "$APP"

echo "==> Fertig: $APP"
echo "    Erststart: Rechtsklick -> Öffnen  (unsigniert/ad-hoc -> Gatekeeper)."
if [[ "$SIGN_ID" == "-" ]]; then
  echo "    Hinweis: ad-hoc signiert -> macOS fragt nach JEDEM Update erneut nach dem"
  echo "    Schlüsselbund. Mit stabiler Identität vermeiden:"
  echo "    CODESIGN_IDENTITY=\"<dein-Codesign-Zert>\" bash build/build.sh"
fi
