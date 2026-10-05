"""mycodex: multi-account orchestration around the official Codex CLI."""

__version__ = "0.3.0"
__author__ = "Manoj Verma"
__github__ = "manojvermamv"
__url__ = "https://github.com/manojvermamv/mycodex"

# Codex internals mycodex relies on (openai_base_url, 426 -> HTTPS fallback, auth.json layout,
# app-server methods) were verified against this release series.
TESTED_CODEX_SERIES = "0.160."
