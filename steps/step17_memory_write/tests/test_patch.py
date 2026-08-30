"""What editing a file must refuse to get wrong.

Every case here was reproduced against real files before it was fixed; the
comments say what was measured rather than what seemed likely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minicodex.patch import (
    Edit,
    apply_edits,
    locate,
    read_source,
    syntax_error,
    write_source,
)

TARGET = '''"""Text helpers."""


def clean_title(text: str) -> str:
    if not text:
        return ""
    return text.strip().title()


def clean_body(text: str) -> str:
    if not text:
        return ""
    return text.strip().lower()
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "textutil.py").write_text(TARGET, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# ambiguity
# ---------------------------------------------------------------------------


def test_an_anchor_appearing_twice_is_refused(repo: Path) -> None:
    """`str.replace()` changes all three identical guards and reports success.
    That is the behaviour this whole module exists to not have."""
    result = apply_edits(
        [
            Edit(
                "textutil.py",
                '    if not text:\n        return ""',
                "    if not text:\n        return None",
            )
        ],
        repo,
    )

    assert "appears 2 times" in result
    assert (repo / "textutil.py").read_text(encoding="utf-8") == TARGET


def test_the_ambiguity_error_names_the_lines(repo: Path) -> None:
    """ "It appears twice" leaves the model guessing how to widen. The line
    numbers are the difference between a useful retry and a coin flip."""
    result = apply_edits(
        [Edit("textutil.py", '    if not text:\n        return ""', "    x")],
        repo,
    )
    assert "on lines 5, 11" in result


def test_widening_the_anchor_makes_it_unique(repo: Path) -> None:
    result = apply_edits(
        [
            Edit(
                "textutil.py",
                'def clean_body(text: str) -> str:\n    if not text:\n        return ""',
                "def clean_body(text: str) -> str:\n    if not text:\n        return None",
            )
        ],
        repo,
    )

    assert result.startswith("Applied")
    after = (repo / "textutil.py").read_text(encoding="utf-8")
    assert after.count("return None") == 1
    assert 'def clean_title(text: str) -> str:\n    if not text:\n        return ""' in after


# ---------------------------------------------------------------------------
# graded matching
# ---------------------------------------------------------------------------


def test_an_exact_unique_match_is_used(repo: Path) -> None:
    result = apply_edits(
        [Edit("textutil.py", "    return text.strip().lower()", "    return text.strip()")], repo
    )
    assert result.startswith("Applied")
    assert ".lower()" not in (repo / "textutil.py").read_text(encoding="utf-8")


def test_trailing_whitespace_the_model_added_is_tolerated() -> None:
    content = 'def f(t):\n    if not t:\n        return ""\n    return t.lower()\n'
    span, why = locate(content, "    return t.lower() ")
    assert why is None
    assert span is not None


def test_indentation_the_model_got_wrong_is_tolerated() -> None:
    content = "def f(t):\n    return t.lower()\n"
    _span, why = locate(content, "        return t.lower()")
    assert why is None


def test_the_exact_level_wins_before_a_relaxed_one_can_see_ambiguity() -> None:
    """Order is load-bearing, not cosmetic.

    The second site is indented with a tab, so a four-space anchor matches
    exactly once but matches twice once indentation is ignored. Trying the
    relaxed level first turns an edit that should succeed into an ambiguity
    error.

    This test exists because reversing `_LEVELS` was mutation-tested and every
    other test in this file stayed green -- the ordering had never actually
    been covered. Writing it also turned up something worth knowing: with two
    space-indented sites, the deeper one *contains* the shallower anchor as a
    substring, so even the exact level reports ambiguity. Nesting makes plain
    substring matching ambiguous on its own.
    """
    content = "def a():\n    return 1\n\ndef b():\n\treturn 1\n"

    span, why = locate(content, "    return 1")

    assert why is None, "the exact, unique match should have been taken"
    assert span is not None
    start, _end = span
    assert content[:start].count("\n") == 1, "matched the wrong site"


def test_a_relaxed_level_still_refuses_ambiguity() -> None:
    """Relaxing whitespace must not relax uniqueness -- that combination is
    how the wrong site gets edited while every check passes."""
    content = "def a():\n    return 1\n\ndef b():\n    return 1\n"
    span, why = locate(content, "        return 1")
    assert span is None
    assert why is not None and "2 times" in why


def test_text_that_is_simply_absent_says_so() -> None:
    span, why = locate("def f():\n    return 1\n", "    return 99")
    assert span is None
    assert why == "that text is not in the file"


# ---------------------------------------------------------------------------
# line endings
# ---------------------------------------------------------------------------


def test_crlf_survives_an_edit(tmp_path: Path) -> None:
    """Measured: read_text() translates CRLF to LF, the match succeeds, and
    write_text() saves LF -- so a one-line edit rewrites every line ending in
    the file and the diff shows the whole file as changed."""
    path = tmp_path / "crlf.py"
    original = b"def f():\r\n    return 1\r\n\r\ndef g():\r\n    return 2\r\n"
    path.write_bytes(original)

    result = apply_edits([Edit("crlf.py", "    return 1", "    return 99")], tmp_path)

    assert result.startswith("Applied")
    after = path.read_bytes()
    assert after.count(b"\r\n") == original.count(b"\r\n")
    assert b"return 99" in after
    assert b"\n" not in after.replace(b"\r\n", b"")


def test_lf_files_stay_lf(tmp_path: Path) -> None:
    path = tmp_path / "lf.py"
    path.write_bytes(b"def f():\n    return 1\n")

    apply_edits([Edit("lf.py", "    return 1", "    return 99")], tmp_path)

    assert b"\r" not in path.read_bytes()


def test_read_source_reports_the_ending(tmp_path: Path) -> None:
    crlf, lf = tmp_path / "a.py", tmp_path / "b.py"
    crlf.write_bytes(b"a\r\nb\r\n")
    lf.write_bytes(b"a\nb\n")

    assert read_source(crlf) == ("a\nb\n", "\r\n")
    assert read_source(lf) == ("a\nb\n", "\n")


def test_write_source_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    write_source(path, "a\nb\n", "\r\n")
    assert path.read_bytes() == b"a\r\nb\r\n"


# ---------------------------------------------------------------------------
# non-ASCII
# ---------------------------------------------------------------------------


def test_cjk_content_does_not_shift_offsets(tmp_path: Path) -> None:
    """Offsets are in characters because everything here is `str`. The same
    code over `bytes` would slice mid-character on the comment above.

    The `noqa: RUF001` below is deliberate. That rule flags characters that
    look like ASCII but are not -- a real problem when a Cyrillic `а` sneaks
    into an English identifier. Here the fullwidth comma is simply the correct
    punctuation for the language, and the point of the test is that it
    survives the round trip byte for byte.

    The `noqa: RUF002` on this docstring is not a second exception to the same
    rule so much as a demonstration of it: writing the sentence above put a
    real Cyrillic `а` in this file, and ruff caught it. The rule earned its
    keep on the paragraph explaining why it was being suppressed.
    """  # noqa: RUF002
    path = tmp_path / "cjk.py"
    comment = "# 去掉首尾空白，保留内部结构"  # noqa: RUF001
    path.write_text(f"{comment}\ndef f():\n    return 1\n", encoding="utf-8")

    result = apply_edits([Edit("cjk.py", "    return 1", "    return 99")], tmp_path)

    assert result.startswith("Applied")
    after = path.read_text(encoding="utf-8")
    assert comment in after
    assert "return 99" in after


def test_the_anchor_itself_can_be_cjk(tmp_path: Path) -> None:
    path = tmp_path / "cjk.py"
    path.write_text("# 旧注释\ndef f():\n    return 1\n", encoding="utf-8")

    apply_edits([Edit("cjk.py", "# 旧注释", "# 新注释")], tmp_path)

    assert "# 新注释" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# all or nothing
# ---------------------------------------------------------------------------


def test_a_failure_in_the_third_edit_writes_nothing(tmp_path: Path) -> None:
    """Measured: writing as it goes left files 1 and 2 edited and 3, 4, 5
    untouched -- a state the model never asked for and cannot see."""
    for i in range(1, 6):
        (tmp_path / f"f{i}.py").write_text(f"VALUE = {i}\n", encoding="utf-8")

    result = apply_edits(
        [
            Edit("f1.py", "VALUE = 1", "VALUE = 100"),
            Edit("f2.py", "VALUE = 2", "VALUE = 200"),
            Edit("f3.py", "VALUE = XX", "VALUE = 300"),
            Edit("f4.py", "VALUE = 4", "VALUE = 400"),
            Edit("f5.py", "VALUE = 5", "VALUE = 500"),
        ],
        tmp_path,
    )

    assert "edit 3 of 5" in result
    for i in range(1, 6):
        assert (tmp_path / f"f{i}.py").read_text(encoding="utf-8") == f"VALUE = {i}\n"


def test_several_edits_to_one_file_all_land(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A = 1\nB = 2\n", encoding="utf-8")

    result = apply_edits(
        [Edit("a.py", "A = 1", "A = 10"), Edit("a.py", "B = 2", "B = 20")], tmp_path
    )

    assert result.startswith("Applied")
    # Known limit: each edit is validated against the file on disk, so two
    # edits to one file are planned independently and the last write wins.
    # Documented rather than fixed -- chapter 4 never observed a model
    # sending two edits to one file, and guessing at the merge semantics
    # before seeing one would be inventing a spec.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "A = 1\nB = 20\n"


def test_an_empty_patch_is_refused(tmp_path: Path) -> None:
    assert "no edits" in apply_edits([], tmp_path)


# ---------------------------------------------------------------------------
# staying inside the repository
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    ["../../../../etc/passwd", "/etc/passwd", "../outside.py", "src/../../outside.py"],
)
def test_an_edit_outside_the_repository_is_refused(tmp_path: Path, escape: str) -> None:
    """The relative spellings went through until chapter 4: chapter 3 branched
    on `is_absolute()` and only checked containment inside that branch, and
    its test used an absolute path, so nothing ever exercised `..`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.py").write_text("SECRET = 1\n", encoding="utf-8")

    result = apply_edits([Edit(escape, "SECRET = 1", "SECRET = 2")], repo)

    assert "outside the repository" in result
    assert (tmp_path / "outside.py").read_text(encoding="utf-8") == "SECRET = 1\n"


def test_a_path_that_stays_inside_via_dotdot_is_allowed(tmp_path: Path) -> None:
    """`src/../a.py` is inside the repository. Rejecting every `..` would be
    simpler and would also reject this."""
    (tmp_path / "src").mkdir()
    (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")

    result = apply_edits([Edit("src/../a.py", "X = 1", "X = 2")], tmp_path)

    assert result.startswith("Applied")


# ---------------------------------------------------------------------------
# leaving the file parseable
# ---------------------------------------------------------------------------


def test_an_edit_that_breaks_the_syntax_is_refused(repo: Path) -> None:
    result = apply_edits(
        [Edit("textutil.py", "def clean_body(text: str) -> str:", "def clean_body(text: str) ->")],
        repo,
    )

    assert "unparseable" in result
    assert (repo / "textutil.py").read_text(encoding="utf-8") == TARGET


def test_the_syntax_error_says_where(repo: Path) -> None:
    result = apply_edits(
        [Edit("textutil.py", "    return text.strip().title()", "    return text.strip(")], repo
    )
    assert "line" in result


def test_non_python_files_are_not_parsed(tmp_path: Path) -> None:
    """We have no parser for these, and refusing to edit them would be worse
    than not checking."""
    path = tmp_path / "notes.md"
    path.write_text("# Title\n\ndef f( :\n", encoding="utf-8")

    result = apply_edits([Edit("notes.md", "# Title", "# Heading")], tmp_path)

    assert result.startswith("Applied")


def test_syntax_error_only_looks_at_python(tmp_path: Path) -> None:
    assert syntax_error(tmp_path / "a.md", "def f( :") is None
    assert syntax_error(tmp_path / "a.py", "def f( :") is not None
