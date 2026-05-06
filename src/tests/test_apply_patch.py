"""Unit tests for apply_patch grammar generator."""

from __future__ import annotations

from agent_bridge.adapters.codex.apply_patch import (
    FileStateCache,
    build_file_state,
    compose_patch_envelope,
    patch_for_delete,
    patch_for_edit,
    patch_for_move,
    patch_for_multi_edit,
    patch_for_write,
    strip_cat_n,
    to_relative,
)
from agent_bridge.canonical.schema import ToolCall, ToolResult


def test_strip_cat_n() -> None:
    text = "   1\thello\n   2\tworld\n"
    assert strip_cat_n(text) == "hello\nworld\n"


def test_strip_cat_n_no_lineno() -> None:
    text = "plain text\nno numbers\n"
    assert strip_cat_n(text) == text


def test_to_relative() -> None:
    assert to_relative("/repo/src/foo.py", "/repo") == "src/foo.py"


def test_patch_for_write_new_file() -> None:
    p = patch_for_write("hello.py", "import os\nprint(os.getcwd())\n", file_existed=False)
    assert p.startswith("*** Begin Patch\n")
    assert "*** Add File: hello.py" in p
    assert "+import os" in p
    assert "+print(os.getcwd())" in p
    assert p.endswith("*** End Patch\n")


def test_patch_for_write_existing_file_uses_delete_add() -> None:
    p = patch_for_write("hello.py", "new content\n", file_existed=True)
    assert "*** Delete File: hello.py" in p
    assert "*** Add File: hello.py" in p


def test_patch_for_edit_with_context() -> None:
    file_content = "line1\nline2\nold\nline4\nline5\n"
    p = patch_for_edit("foo.py", "old", "new", file_content=file_content)
    assert "*** Update File: foo.py" in p
    assert "-old" in p
    assert "+new" in p
    # context lines should appear (with leading space)
    assert " line1" in p or " line2" in p


def test_patch_for_edit_without_context() -> None:
    p = patch_for_edit("foo.py", "old", "new", file_content=None)
    assert "*** Update File: foo.py" in p
    assert "-old" in p
    assert "+new" in p


def test_patch_for_multi_edit() -> None:
    file_content = "AAA\nBBB\nCCC\n"
    edits = [
        {"old": "AAA", "new": "aaa"},
        {"old": "CCC", "new": "ccc"},
    ]
    p = patch_for_multi_edit("foo.py", edits, file_content=file_content)
    # Two hunks expected
    assert p.count("@@") == 2
    assert "-AAA" in p and "+aaa" in p
    assert "-CCC" in p and "+ccc" in p


def test_patch_for_delete() -> None:
    p = patch_for_delete("foo.py")
    assert "*** Delete File: foo.py" in p


def test_patch_for_move() -> None:
    p = patch_for_move("old.py", "new.py")
    assert "*** Update File: old.py" in p
    assert "*** Move to: new.py" in p


def test_build_file_state_from_read() -> None:
    moments = [
        ToolCall(ts="2026-01-01T00:00:00.000Z", tool="read_file", call_id="r1", args={"path": "/p/foo.py"}),
        ToolResult(ts="2026-01-01T00:00:01.000Z", call_id="r1", output_text="   1\tline a\n   2\tline b\n"),
        ToolCall(ts="2026-01-01T00:00:02.000Z", tool="edit_file", call_id="e1",
                 args={"path": "/p/foo.py", "old": "line a", "new": "LINE A"}),
    ]
    snaps = build_file_state(moments)
    assert "e1" in snaps
    assert snaps["e1"].get("/p/foo.py") == "line a\nline b\n"


def test_build_file_state_from_write_then_edit() -> None:
    moments = [
        ToolCall(ts="2026-01-01T00:00:00.000Z", tool="write_file", call_id="w1",
                 args={"path": "/p/foo.py", "content": "x\ny\n"}),
        ToolCall(ts="2026-01-01T00:00:01.000Z", tool="edit_file", call_id="e1",
                 args={"path": "/p/foo.py", "old": "x", "new": "X"}),
    ]
    snaps = build_file_state(moments)
    # Snapshot for e1 should contain the post-write content (before the edit)
    assert snaps["e1"].get("/p/foo.py") == "x\ny\n"
    # write_file's own snapshot should be the empty pre-state
    assert snaps["w1"].get("/p/foo.py") is None
