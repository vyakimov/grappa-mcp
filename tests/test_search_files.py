async def test_basic_search(call, tmp_path):
    (tmp_path / "a.py").write_text("import os\nprint('hello')\n")
    (tmp_path / "b.py").write_text("import sys\n")
    result = await call("search_files", pattern=r"import \w+", path=str(tmp_path))
    assert result["truncated"] is False
    assert {(m["file"], m["line"]) for m in result["matches"]} == {
        (str(tmp_path / "a.py"), 1),
        (str(tmp_path / "b.py"), 1),
    }


async def test_glob_filter(call, tmp_path):
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "a.txt").write_text("needle\n")
    result = await call("search_files", pattern="needle", path=str(tmp_path), glob="*.py")
    assert [m["file"] for m in result["matches"]] == [str(tmp_path / "a.py")]


async def test_case_insensitive(call, tmp_path):
    (tmp_path / "a.txt").write_text("NEEDLE\n")
    result = await call("search_files", pattern="needle", path=str(tmp_path))
    assert result["matches"] == []
    result = await call(
        "search_files", pattern="needle", path=str(tmp_path), case_sensitive=False
    )
    assert len(result["matches"]) == 1


async def test_invalid_regex(call, tmp_path):
    result = await call("search_files", pattern="(unclosed", path=str(tmp_path))
    assert "invalid regex" in result["error"]


async def test_skips_binary_and_vcs_dirs(call, tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"needle\0\x01\x02")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("needle\n")
    (tmp_path / "src.txt").write_text("needle\n")
    result = await call("search_files", pattern="needle", path=str(tmp_path))
    assert [m["file"] for m in result["matches"]] == [str(tmp_path / "src.txt")]


async def test_max_results_truncation(call, tmp_path):
    (tmp_path / "many.txt").write_text("needle\n" * 50)
    result = await call("search_files", pattern="needle", path=str(tmp_path), max_results=10)
    assert len(result["matches"]) == 10
    assert result["truncated"] is True


async def test_single_file_target(call, tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("one\nneedle here\n")
    result = await call("search_files", pattern="needle", path=str(p))
    assert result["matches"] == [{"file": str(p), "line": 2, "text": "needle here"}]


async def test_missing_path(call, tmp_path):
    result = await call("search_files", pattern="x", path=str(tmp_path / "nope"))
    assert "path not found" in result["error"]
