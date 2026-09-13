"""The page is one string, and its failure mode is total.

`$("#typo")` is `null`, `null.addEventListener` throws, and a top-level throw aborts the
whole script -- so the search form loses its submit handler and the browser falls back to a
native GET. The page still renders and the access log still says 200. Nothing about that
looks like an error, which is why it is worth a test rather than a careful eye.
"""

from __future__ import annotations

import re

from ghlore.api.ui import page

HTML = page()

ELEMENT_IDS = set(re.findall(r'id="([^"]+)"', HTML))
SELECTED_IDS = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', HTML))


def test_every_selector_matches_an_element() -> None:
    missing = SELECTED_IDS - ELEMENT_IDS
    assert not missing, f'$("#id") with no element in the markup: {sorted(missing)}'


def test_the_why_examples_name_a_repository_and_a_line() -> None:
    """`why` blames a clone, which is per repository: a path from one is a 404 against
    another, and an example that does not land teaches nothing."""
    examples = re.findall(r'\{label: "[^"]+",\s*at: "([^"]+)", repo: "([^"]+)"\}', HTML)

    assert examples, "the why samples are gone or no longer parseable"
    for at, repo in examples:
        path, _, line = at.rpartition(":")
        assert path.endswith(".py"), at
        assert line.isdigit() and int(line) >= 1, at
        assert "/" in repo, repo


def test_the_page_carries_its_own_version() -> None:
    """The handshake refuses a client of another version, and the page is a client."""
    from ghlore import __version__

    assert f'"{__version__}"' in HTML
    assert "/*VERSION*/" not in HTML
    assert "/*FAVICON*/" not in HTML
