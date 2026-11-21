"""Client server: a single HTML page + WebSocket on localhost:9002.

Serves one page at `/`. That page holds an iframe and a WebSocket; visiting it
starts the flow (open iCloud -> click Sign In) automatically.
"""

__version__ = "0.1.0"

DEFAULT_PORT = 9002
