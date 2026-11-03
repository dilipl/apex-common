"""The one definition of how a filename folds into its searchable form.

WHAT IT IS
    "World_War_2.pdf" -> "world war 2 pdf". Lowercased, with every run of
    separators collapsed to a single space.

WHY IT MATTERS MORE THAN IT LOOKS
    ingestor-service writes this value into `file_name_norm` on every upload.
    task-service-agent searches it with a CASE-SENSITIVE ANCHORED regex
    (`^prefix`), because that is the only regex form MongoDB can answer from an
    index -- `$options: "i"`, or an unanchored pattern, is a collection scan on
    every keystroke, which does not survive a corpus of millions.

    So both sides must fold identically. If they drift, nothing errors: the
    query simply stops matching rows that are sitting right there, and title
    search quietly goes blind for exactly the filenames the two rules disagree
    about.

IT HAD ALREADY DRIFTED
    Three copies existed, not two, and one of them was DIFFERENT:

        ingestor upload_models.py   re.sub(r"[\\s_\\-.]+", " ", ...)   <- stored
        task-agent documents.py     re.sub(r"[\\s_\\-.]+", " ", ...)   <- queried
        task-agent eval/dataset.py  re.sub(r"[^a-z0-9]+", " ", ...)   <- DIVERGED

    The third collapses EVERY non-alphanumeric character, so it disagreed with
    the stored form on any filename containing punctuation beyond space, `_`,
    `-` and `.`. One file in the live corpus already triggered it:

        "Unforgettable Calls Vol. 1 Dave Ramsey's Greatest Hits -1hr.mp4"
            stored:  unforgettable calls vol 1 dave ramsey's greatest hits 1hr mp4
            eval:    unforgettable calls vol 1 dave ramsey s greatest hits 1hr mp4
                                                            ^^^ apostrophe folded

    Its docstring said "Mirrors the ingestor's normalisation", which was simply
    untrue. The eval harness got away with it because it normalises BOTH sides
    of its comparison with its own copy, so the mismatch was latent rather than
    broken -- the kind of bug that surfaces the day someone compares an eval key
    against a stored one.

THE STORED RULE WINS
    `[\\s_\\-.]+` is canonical because it is what is already in the database.
    Changing the rule means re-normalising every row
    (`scripts/migrate_documents_title_index.py`), so the aggressive variant was
    the one to retire, not the one to adopt. Verified before switching: all 19
    corpus rows' `file_name_norm` match this rule exactly, and every eval
    expectation still resolves under it.

THIS IS THE SINGLE HOME
    Until 2026-11-03 this file was copied into each service and held in place by
    a hand-written identical fixture table per repo, because the services shared
    no library. They now share this one. Each service keeps a REPO GUARD
    asserting it defines nothing here itself — re-adding a local copy is the
    failure this package exists to prevent.

CHANGING THE RULE IS A DATA MIGRATION, NOT AN EDIT
    The STORED rule is canonical: ingestor-service writes `file_name_norm` on
    upload, and title search matches it with an anchored case-sensitive regex.
    So changing this means, in order: bump TITLE_NORM_CONTRACT, update the
    fixture table in tests/, and run
    `python -m scripts.migrate_documents_title_index` in task-service-agent.
    Skipping the last step leaves every pre-existing document unsearchable by
    title.
"""

from __future__ import annotations

import re

TITLE_NORM_FIELD = "file_name_norm"
"""Name of the derived, indexed field stored alongside `file_name`."""

TITLE_NORM_PATTERN = r"[\s_\-.]+"
"""Separator run. Users type spaces; files are named with underscores, hyphens
or dots ("world_war_2.pdf"), so treating them as interchangeable is what makes
title search behave the way people expect."""

TITLE_NORM_CONTRACT = "v1:collapse-separators:strip-lower"
"""Identifier for the folding rule, pinned by a test in every repo that
implements it. Bump it only together with the fixture tables -- it exists so a
change cannot be made in one service and forgotten in the other."""

_SEPARATORS = re.compile(TITLE_NORM_PATTERN)


def normalize_title(value: str | None) -> str:
    """Fold a filename (or a search query) into its `file_name_norm` form.

        "world_war_2.pdf"  ->  "world war 2 pdf"
        "World  War"       ->  "world war"

    Both sides of a comparison go through this, which is what lets the Mongo
    query be CASE-SENSITIVE -- and a case-sensitive anchored regex is the only
    kind MongoDB can answer from an index.

    Note what is deliberately NOT folded: apostrophes, parentheses, commas,
    ampersands and accented letters all survive. A filename is a fact, so a
    query has to spell it the way it is spelled; folding punctuation away would
    make "Ramseys" match "Ramsey's" at the cost of the stored form and the
    query form no longer agreeing.
    """
    return _SEPARATORS.sub(" ", (value or "").strip().lower()).strip()
