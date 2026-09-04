#!/bin/sh
gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:${PORT:-8000} --timeout 120 --access-logfile - --error-logfile - app.main:app
