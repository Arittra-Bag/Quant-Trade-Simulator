"""
Gunicorn settings, read automatically by `gunicorn app:server` from this directory.

Sized for one small instance (a free tier's 512 MB and a fraction of a CPU):

- One worker. The cached book, the latency history and the advisor's rate limiter live in
  the process, and a second copy of the app would double memory for no gain on one CPU.
- Threads, so a Start click or a slow advisor call runs beside the polls instead of queueing
  behind them, as it did on the default single-threaded worker.
- Keep-alive longer than the poll interval, so polls reuse one connection.

Binds to $PORT when it is set, as hosts like Render set it, and to 8050 like `python app.py`
otherwise.
"""
import os

bind = [f"0.0.0.0:{os.environ.get('PORT', '8050')}"]
# Fixed, not read from WEB_CONCURRENCY: some hosts set that, and a second worker would split
# the advisor's rate limiter and the cached book in two.
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
keepalive = 30
timeout = 60  # a stuck worker is restarted; long requests run on threads and do not trip it
graceful_timeout = 10
accesslog = None  # a poll every 500 ms would bury the feed client's log
