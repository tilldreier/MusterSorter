#!/bin/bash
# Wrapper fuer den LaunchAgent: launchd/xpcproxy verweigert auf manchen
# macOS-Versionen das direkte Ausfuehren von Drittanbieter-Binaries als
# ProgramArguments (posix_spawn schlaegt mit EACCES fehl, obwohl dieselbe
# Datei per SSH/Terminal normal ausfuehrbar ist). Der Umweg ueber /bin/bash
# (ein von Apple signiertes System-Binary) umgeht das zuverlaessig.
set -e
cd "$(dirname "$0")/.."

# WICHTIG: Flask direkt starten (app.py's eigener app.run()), NICHT ueber
# gunicorn - siehe freisteller/deploy/run.sh fuer die ausfuehrliche
# Begruendung (gunicorns fork() verursachte OneDrive-Deadlocks).
exec .venv/bin/python3 app.py
