#!/usr/bin/env python3
"""Regression tests for decomp provenance stamping in sync_from_decomp."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import sync_from_decomp


def test_pin_url_to_commit_pins_branch_to_commit():
    """A branch URL resolves to its commit and reports its repo slug."""
    original = sync_from_decomp.resolve_branch_head
    sync_from_decomp.resolve_branch_head = lambda repo, ref: "abc123"
    try:
        url, repo_slug, commit = sync_from_decomp.pin_url_to_commit(
            "https://raw.githubusercontent.com/pret/pokeheartgold/master/asm/macros/script.inc"
        )
    finally:
        sync_from_decomp.resolve_branch_head = original

    assert (
        url
        == "https://raw.githubusercontent.com/pret/pokeheartgold/abc123/asm/macros/script.inc"
    ), url
    assert repo_slug == "pret/pokeheartgold", repo_slug
    assert commit == "abc123", commit


def test_pin_url_to_commit_leaves_other_urls_alone():
    """Non-GitHub-raw URLs pass through with empty provenance."""
    url = "https://example.com/data.json"

    pinned, repo_slug, commit = sync_from_decomp.pin_url_to_commit(url)

    assert pinned == url, pinned
    assert repo_slug == "", repo_slug
    assert commit == "", commit


def test_decomp_provenance_reports_source_repo_and_commit():
    """Provenance comes from the configured decomp source URLs."""
    original = sync_from_decomp.resolve_branch_head
    sync_from_decomp.resolve_branch_head = lambda repo, ref: "def456"
    try:
        repo_slug, commit = sync_from_decomp.decomp_provenance(
            {
                "scrcmd": "https://raw.githubusercontent.com/pret/pokeheartgold/master/asm/macros/script.inc",
                "movement": "https://raw.githubusercontent.com/pret/pokeheartgold/master/asm/macros/movement.inc",
            }
        )
    finally:
        sync_from_decomp.resolve_branch_head = original

    assert repo_slug == "pret/pokeheartgold", repo_slug
    assert commit == "def456", commit
