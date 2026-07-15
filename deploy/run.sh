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
#
# PYTHONUNBUFFERED=1: ohne Terminal (StandardOutPath/StandardErrorPath sind
# Dateien) puffert Python stdout blockweise statt zeilenweise - print()-
# Warnungen aus dem Hintergrund-Thread (_generate_suggestions) landeten
# dadurch minutenlang unsichtbar im Puffer statt sofort im Log zu erscheinen
# (konkret beobachtet: ein Anthropic-400-Fehler wurde erst nach Prozessende
# sichtbar). Mit PYTHONUNBUFFERED erscheinen print()-Ausgaben sofort.
export PYTHONUNBUFFERED=1
exec .venv/bin/python3 app.py
