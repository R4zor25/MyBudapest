from __future__ import annotations


class DigestError(Exception):
    """Base of every error this project raises deliberately. The per-source `except` in the
    run loop catches this, not `Exception`, so a bug in our own code still fails loudly."""


class FetchError(DigestError):
    """A source could not be retrieved: transport failure, timeout, or an error status."""


class ParseError(DigestError):
    """A response arrived but could not be turned into events — typically selector drift."""


class ConfigError(DigestError):
    """A config file, source descriptor, or the profile secret is missing or invalid."""


class DeliveryError(DigestError):
    """A configured deliverer could not send: missing credentials or a transport failure."""
