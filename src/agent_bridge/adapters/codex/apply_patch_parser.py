"""Reverse parser for Codex's apply_patch grammar.

The grammar (codex-rs/tools/src/tool_apply_patch.lark):

    *** Begin Patch
    *** Add File: <path>
    +<line>...
    *** Update File: <path>
    *** Move to: <new path>?
    @@ <header>?
     <ctx>
    -<removed>
    +<added>
    *** Delete File: <path>
    *** End Patch

We parse leniently — Codex's parser tolerates Add/Delete/Update in any order
inside one envelope, with multiple hunks per Update File.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class HunkLine:
    op: Literal[" ", "+", "-"]
    text: str


@dataclass
class Hunk:
    header: str | None = None
    lines: list[HunkLine] = field(default_factory=list)

    @property
    def removed(self) -> list[str]:
        return [l.text for l in self.lines if l.op == "-"]

    @property
    def added(self) -> list[str]:
        return [l.text for l in self.lines if l.op == "+"]

    @property
    def context_before(self) -> list[str]:
        out: list[str] = []
        for l in self.lines:
            if l.op == " ":
                out.append(l.text)
            else:
                break
        return out


@dataclass
class FileOp:
    kind: Literal["add", "delete", "update"]
    path: str
    move_to: str | None = None
    add_lines: list[str] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)


class ApplyPatchParseError(ValueError):
    pass


def parse_apply_patch(patch_text: str) -> list[FileOp]:
    """Parse an apply_patch input string into a list of FileOp records.

    Raises ApplyPatchParseError if the envelope or grammar is malformed.
    """
    lines = patch_text.splitlines()
    # Strip everything before "*** Begin Patch" and after "*** End Patch"
    start = next((i for i, l in enumerate(lines) if l.strip() == "*** Begin Patch"), None)
    end = next((i for i, l in enumerate(lines) if l.strip() == "*** End Patch"), None)
    if start is None or end is None:
        raise ApplyPatchParseError("missing *** Begin Patch / *** End Patch envelope")
    body = lines[start + 1 : end]

    ops: list[FileOp] = []
    i = 0
    while i < len(body):
        line = body[i]
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):]
            content_lines: list[str] = []
            i += 1
            while i < len(body) and not body[i].startswith("*** "):
                if body[i].startswith("+"):
                    content_lines.append(body[i][1:])
                # tolerate empty / non-prefixed lines
                i += 1
            ops.append(FileOp(kind="add", path=path, add_lines=content_lines))
        elif line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):]
            ops.append(FileOp(kind="delete", path=path))
            i += 1
        elif line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):]
            move_to: str | None = None
            i += 1
            if i < len(body) and body[i].startswith("*** Move to: "):
                move_to = body[i][len("*** Move to: "):]
                i += 1
            hunks: list[Hunk] = []
            cur: Hunk | None = None
            while i < len(body) and not body[i].startswith("*** "):
                if body[i].startswith("@@"):
                    if cur is not None:
                        hunks.append(cur)
                    header = body[i][2:].lstrip() or None
                    cur = Hunk(header=header, lines=[])
                elif body[i] == "*** End of File":
                    # apply_patch EOF marker — ignore for our purposes
                    pass
                elif body[i] and body[i][0] in (" ", "+", "-"):
                    if cur is None:
                        cur = Hunk(header=None, lines=[])
                    cur.lines.append(HunkLine(op=body[i][0], text=body[i][1:]))
                # silently skip other lines (lenient mode)
                i += 1
            if cur is not None:
                hunks.append(cur)
            ops.append(FileOp(kind="update", path=path, move_to=move_to, hunks=hunks))
        else:
            # unknown control line; advance
            i += 1

    return ops
