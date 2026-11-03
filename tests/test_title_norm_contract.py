"""The title-folding contract, pinned.

WHY A TEST AND NOT JUST A COMMENT
    ingestor-service WRITES `file_name_norm`; this service QUERIES it with a
    case-sensitive anchored regex. If the two foldings drift, nothing errors —
    the query simply stops matching rows that are sitting right there, and title
    search goes blind for exactly the filenames the two rules disagree about.
    A comment saying "keep in sync" does not catch that. This does.

    This table used to be duplicated, byte-for-byte, in every service that
    folded a filename — the only mechanism available while the services shared
    no library. It now lives here once. Each service keeps a REPO GUARD
    (tests/test_shared_modules.py) asserting it defines no folding rule of its
    own.

IT HAD ALREADY DRIFTED, WHICH IS WHY THIS EXISTS
    Three copies existed and the third was a different rule: the eval harness
    collapsed EVERY non-alphanumeric character while the stored form collapses
    only whitespace, `_`, `-` and `.`. Its docstring claimed it mirrored the
    ingestor. The apostrophe cases below are that bug, turned into a test.
"""

from __future__ import annotations

import pytest

from apex_common.title_norm import (
    TITLE_NORM_CONTRACT,
    TITLE_NORM_FIELD,
    TITLE_NORM_PATTERN,
    normalize_title,
)

# ---------------------------------------------------------------------------
# THE CONTRACT TABLE — identical in both repos. Change it in both, or not at all.
# ---------------------------------------------------------------------------
CASES: list[tuple[str | None, str]] = [
    # The canonical example, and the form it is stored as.
    ("World_War_2.pdf", "world war 2 pdf"),
    # Idempotent: folding an already-folded value must not move it, because a
    # typed query and a stored filename both pass through here.
    ("world war 2 pdf", "world war 2 pdf"),
    # Separator runs collapse to exactly one space.
    ("World  War", "world war"),
    ("file__name", "file name"),
    ("report...final.pdf", "report final pdf"),
    ("a-b_c.d e", "a b c d e"),
    # Every separator kind, including whitespace that is not a space.
    ("a\tb\nc", "a b c"),
    # Leading and trailing separator runs disappear entirely.
    ("--leading.pdf", "leading pdf"),
    ("trailing-", "trailing"),
    ("  padded  .pdf ", "padded pdf"),
    # Case folds; the Mongo regex is case-SENSITIVE precisely because of this.
    ("MixedCASE-File_NAME.PDF", "mixedcase file name pdf"),
    # Digits and dotted dates are separators like any other.
    ("2026.10.13 notes.txt", "2026 10 13 notes txt"),
    # NOT FOLDED, deliberately — this is the half that had drifted. A filename
    # is a fact, so a query spells it the way it is spelled; folding these away
    # would mean the stored form and the query form no longer agree.
    ("Unforgettable Calls Vol. 1 Dave Ramsey's Greatest Hits -1hr.mp4",
     "unforgettable calls vol 1 dave ramsey's greatest hits 1hr mp4"),
    ("Q1 (final).pdf", "q1 (final) pdf"),
    ("A, B & C.docx", "a, b & c docx"),
    ("Café Menu.pdf", "café menu pdf"),
    ("100% margin.xlsx", "100% margin xlsx"),
    # Degenerate inputs must be "" rather than raising: this runs inside a
    # pydantic validator on the write path and on every keystroke on the read
    # path.
    ("", ""),
    (None, ""),
    ("   ", ""),
    ("___", ""),
]


@pytest.mark.parametrize("value,expected", CASES)
def test_the_folding_rule(value, expected):
    assert normalize_title(value) == expected


@pytest.mark.parametrize("value,expected", CASES)
def test_folding_is_idempotent(value, expected):
    """A stored value re-folded must not change. The write path folds once and
    the read path folds the query; a non-idempotent rule would make the second
    pass disagree with the first."""
    assert normalize_title(expected) == expected


def test_punctuation_beyond_separators_survives():
    """The exact regression. The eval harness's copy collapsed every
    non-alphanumeric character, so it produced "ramsey s" where the stored form
    has "ramsey's" — a filename in the live corpus already hit this."""
    assert normalize_title("Ramsey's") == "ramsey's"
    assert normalize_title("Ramsey's") != "ramsey s"


def test_the_contract_identifier_is_pinned():
    """Bumping the rule means bumping this, updating the table in BOTH repos,
    and re-normalising existing rows
    (task-service-agent/scripts/migrate_documents_title_index.py). Rows
    written under the old rule are unsearchable by title until that runs."""
    assert TITLE_NORM_CONTRACT == "v1:collapse-separators:strip-lower"
    assert TITLE_NORM_PATTERN == r"[\s_\-.]+"
    assert TITLE_NORM_FIELD == "file_name_norm"


