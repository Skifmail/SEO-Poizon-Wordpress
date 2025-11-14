"""
Gunicorn configuration file.

This file is used by Gunicorn to configure the web server.
Command to run: gunicorn --config gunicorn.conf.py web_app:app
"""
import os
import multiprocessing

# --- Server Socket ---
# Bind to 0.0.0.0 to be accessible from outside the container.
# The port can be overridden by the PORT environment variable, which is common on hosting platforms.
port = os.environ.get("PORT", "8000")
bind = f"0.0.0.0:{port}"

# --- Worker Processes ---
# A good starting point is (2 * number_of_cores) + 1.
# Heroku/Reg.ru might have recommendations for this value.
workers = multiprocessing.cpu_count() * 2 + 1

# --- Worker Class ---
# Using 'gevent' for better performance with I/O-bound tasks (like API calls).
# 'gevent' must be installed (pip install gevent).
worker_class = "gevent"

# --- Logging ---
# Log to stdout and stderr. Hosting platforms typically collect these logs.
accesslog = "-"
errorlog = "-"

# --- Process Naming ---
# Helps in identifying the process in tools like `ps` or `top`.
proc_name = "seo-poizon-wordpress"

# --- Timeout ---
# Workers silent for more than this many seconds are killed and restarted.
# Increased to 120 seconds because GigaChat processing can be slow.
timeout = 120
