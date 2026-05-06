"""Adapter registry."""

from . import claude_code as claude_code
from . import codex as codex

ADAPTERS = {
    "claude-code": claude_code,
    "codex": codex,
}
