import hashlib
import os

import server


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- write_file ---


async def test_write_and_read_roundtrip(call, tmp_path):
    p = tmp_path / "hello.txt"
    result = await call("write_file", path=str(p), content="héllo wörld ✨\n")
    assert result["sha256"] == sha("héllo wörld ✨\n")
    assert p.read_text(encoding="utf-8") == "héllo wörld ✨\n"


async def test_write_refuses_existing_without_overwrite(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("original")
    result = await call("write_file", path=str(p), content="new")
    assert "already exists" in result["error"]
    assert p.read_text() == "original"

    result = await call("write_file", path=str(p), content="new", overwrite=True)
    assert "error" not in result
    assert p.read_text() == "new"


async def test_write_create_dirs(call, tmp_path):
    p = tmp_path / "a" / "b" / "f.txt"
    result = await call("write_file", path=str(p), content="x", create_dirs=True)
    assert "error" not in result
    assert p.read_text() == "x"


async def test_write_missing_parent_gives_clear_error(call, tmp_path):
    p = tmp_path / "missing" / "f.txt"
    result = await call("write_file", path=str(p), content="x")
    assert "parent directory does not exist" in result["error"]


async def test_write_to_directory_path_fails(call, tmp_path):
    result = await call("write_file", path=str(tmp_path), content="x")
    assert "is a directory" in result["error"]


async def test_overwrite_preserves_permissions(call, tmp_path):
    p = tmp_path / "script.sh"
    p.write_text("echo old")
    p.chmod(0o755)
    result = await call("write_file", path=str(p), content="echo new", overwrite=True)
    assert "error" not in result
    assert oct(p.stat().st_mode & 0o777) == oct(0o755)


async def test_relative_path_resolves_against_default_cwd(call, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DEFAULT_CWD", str(tmp_path))
    result = await call("write_file", path="rel.txt", content="x")
    assert result["path"] == str(tmp_path / "rel.txt")
    assert (tmp_path / "rel.txt").read_text() == "x"


# --- read_file ---


async def test_read_missing_file(call, tmp_path):
    result = await call("read_file", path=str(tmp_path / "nope.txt"))
    assert "file not found" in result["error"]


async def test_read_whole_file(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("one\ntwo\nthree\n")
    result = await call("read_file", path=str(p))
    assert result["content"] == "one\ntwo\nthree\n"
    assert result["total_lines"] == 3
    assert result["lines_returned"] == 3
    assert result["truncated"] is False
    assert result["sha256"] == sha("one\ntwo\nthree\n")


async def test_read_line_offset_and_limit(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("".join(f"line{i}\n" for i in range(1, 11)))
    result = await call("read_file", path=str(p), offset=3, limit=2)
    assert result["content"] == "line3\nline4\n"
    assert result["offset"] == 3
    assert result["lines_returned"] == 2
    assert result["total_lines"] == 10
    assert result["truncated"] is True


async def test_read_offset_past_end(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a\nb\n")
    result = await call("read_file", path=str(p), offset=100)
    assert result["content"] == ""
    assert result["lines_returned"] == 0
    assert result["truncated"] is False


async def test_read_rejects_huge_file(call, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_READ_BYTES", 10)
    p = tmp_path / "big.txt"
    p.write_text("x" * 100)
    result = await call("read_file", path=str(p))
    assert "file too large" in result["error"]


# --- edit_file ---


async def test_edit_single_replacement(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello world\n")
    result = await call("edit_file", path=str(p), old_text="world", new_text="there")
    assert result["replacements"] == 1
    assert result["sha256"] == sha("hello there\n")
    assert p.read_text() == "hello there\n"


async def test_edit_old_string_alias(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello world\n")
    result = await call("edit_file", path=str(p), old_string="world", new_string="there")
    assert result["replacements"] == 1
    assert p.read_text() == "hello there\n"


async def test_edit_requires_old_text(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x")
    result = await call("edit_file", path=str(p), new_text="y")
    assert "old_text" in result["error"]


async def test_edit_not_found(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    result = await call("edit_file", path=str(p), old_text="nope", new_text="x")
    assert "not found in file" in result["error"]


async def test_edit_ambiguous_without_replace_all(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("aa aa\n")
    result = await call("edit_file", path=str(p), old_text="aa", new_text="bb")
    assert "ambiguous" in result["error"]
    assert p.read_text() == "aa aa\n"

    result = await call("edit_file", path=str(p), old_text="aa", new_text="bb", replace_all=True)
    assert result["replacements"] == 2
    assert p.read_text() == "bb bb\n"


async def test_edit_expected_sha256_match(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    read = await call("read_file", path=str(p))
    result = await call(
        "edit_file", path=str(p), old_text="hello", new_text="bye",
        expected_sha256=read["sha256"],
    )
    assert "error" not in result
    assert p.read_text() == "bye\n"


async def test_edit_expected_sha256_mismatch(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    result = await call(
        "edit_file", path=str(p), old_text="hello", new_text="bye",
        expected_sha256="0" * 64,
    )
    assert "sha256 mismatch" in result["error"]
    assert result["actual_sha256"] == sha("hello\n")
    assert p.read_text() == "hello\n"


async def test_edit_preserves_permissions(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    p.chmod(0o604)
    await call("edit_file", path=str(p), old_text="hello", new_text="bye")
    assert oct(p.stat().st_mode & 0o777) == oct(0o604)


# --- list_directory ---


async def test_list_basic(call, tmp_path):
    (tmp_path / "b.txt").write_text("bb")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    result = await call("list_directory", path=str(tmp_path))
    assert [e["name"] for e in result["entries"]] == ["a.txt", "b.txt", "sub"]
    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["a.txt"] == {"name": "a.txt", "type": "file", "size": 1}
    assert by_name["sub"]["type"] == "dir"
    assert by_name["sub"]["size"] is None


async def test_list_missing_directory(call, tmp_path):
    result = await call("list_directory", path=str(tmp_path / "nope"))
    assert "directory not found" in result["error"]


async def test_list_recursive_and_glob(call, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_text("x")
    (tmp_path / "y.py").write_text("y")
    (tmp_path / "z.txt").write_text("z")
    result = await call("list_directory", path=str(tmp_path), recursive=True, glob="*.py")
    assert [e["name"] for e in result["entries"]] == ["sub/x.py", "y.py"]


async def test_list_survives_broken_symlink(call, tmp_path):
    (tmp_path / "real.txt").write_text("x")
    os.symlink(tmp_path / "gone.txt", tmp_path / "dangling")
    result = await call("list_directory", path=str(tmp_path))
    assert "error" not in result
    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["dangling"]["type"] == "symlink"
    assert by_name["real.txt"]["type"] == "file"


# --- file_stat ---


async def test_file_stat(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n")
    p.chmod(0o640)
    result = await call("file_stat", path=str(p))
    assert result["exists"] is True
    assert result["type"] == "file"
    assert result["size"] == 6
    assert result["mode"] == "0640"
    assert result["sha256"] == sha("hello\n")


async def test_file_stat_missing(call, tmp_path):
    result = await call("file_stat", path=str(tmp_path / "nope"))
    assert result["exists"] is False


async def test_file_stat_dir_and_symlink(call, tmp_path):
    result = await call("file_stat", path=str(tmp_path))
    assert result["type"] == "dir"
    os.symlink(tmp_path / "gone", tmp_path / "link")
    result = await call("file_stat", path=str(tmp_path / "link"))
    assert result["type"] == "symlink"


# --- delete_file ---


async def test_delete_file(call, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x")
    result = await call("delete_file", path=str(p))
    assert result["deleted"] is True
    assert not p.exists()


async def test_delete_missing(call, tmp_path):
    result = await call("delete_file", path=str(tmp_path / "nope"))
    assert "path not found" in result["error"]


async def test_delete_nonempty_dir_requires_recursive(call, tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "f.txt").write_text("x")
    result = await call("delete_file", path=str(d))
    assert "recursive" in result["error"]
    assert d.exists()

    result = await call("delete_file", path=str(d), recursive=True)
    assert result["deleted"] is True
    assert not d.exists()


async def test_delete_refuses_protected_paths(call):
    result = await call("delete_file", path="/", recursive=True)
    assert "protected path" in result["error"]


async def test_delete_symlink_not_target(call, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep me")
    link = tmp_path / "link"
    os.symlink(target, link)
    result = await call("delete_file", path=str(link))
    assert result["deleted"] is True
    assert not link.is_symlink()
    assert target.exists()


# --- move_file ---


async def test_move_rename(call, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x")
    dst = tmp_path / "b.txt"
    result = await call("move_file", src=str(src), dst=str(dst))
    assert result["dst"] == str(dst)
    assert not src.exists()
    assert dst.read_text() == "x"


async def test_move_into_directory(call, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x")
    d = tmp_path / "sub"
    d.mkdir()
    result = await call("move_file", src=str(src), dst=str(d))
    assert result["dst"] == str(d / "a.txt")
    assert (d / "a.txt").read_text() == "x"


async def test_move_refuses_overwrite(call, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("new")
    dst = tmp_path / "b.txt"
    dst.write_text("old")
    result = await call("move_file", src=str(src), dst=str(dst))
    assert "already exists" in result["error"]
    assert dst.read_text() == "old"

    result = await call("move_file", src=str(src), dst=str(dst), overwrite=True)
    assert "error" not in result
    assert dst.read_text() == "new"


async def test_move_missing_source(call, tmp_path):
    result = await call("move_file", src=str(tmp_path / "nope"), dst=str(tmp_path / "x"))
    assert "source not found" in result["error"]
