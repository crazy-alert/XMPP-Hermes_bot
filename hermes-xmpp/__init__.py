"""Hermes plugin entry point for the XMPP platform."""

from .adapter import register

__all__ = ["register"]
