#!/bin/sh
set -e

echo ">>> DB-Upgrade..."
flask --app run upgrade-db

echo ">>> Starte Gunicorn..."
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 run:app
