"""Forge: a local job-search tool."""
import logging

# httpx logs the full URL of every request at INFO level, and Adzuna's key travels in its query string.
# Keep these loggers quiet so a secret can never reach a log, whatever the logging setup is.
for _name in ("httpx", "httpx2", "httpcore", "httpcore2"):
    logging.getLogger(_name).setLevel(logging.WARNING)
