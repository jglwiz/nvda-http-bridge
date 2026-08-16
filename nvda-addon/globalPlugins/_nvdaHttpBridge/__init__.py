"""Implementation package for NVDA HTTP Bridge.

This package intentionally avoids importing NVDA modules at import time.  The
thin global plugin entry point owns the NVDA-specific adapter and lifecycle.
"""

from .config import PLUGIN_VERSION, PROTOCOL_VERSION

__all__ = ["PLUGIN_VERSION", "PROTOCOL_VERSION"]
