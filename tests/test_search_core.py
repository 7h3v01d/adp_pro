# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Search core: models, dedupe, and ranking."""
from datetime import timedelta

import pytest

from adp.search.models import (
    SearchQuery, SearchResult, SourceHit, infohash_from_magnet, normalize_infohash, utcnow,
)
from adp.search.service import dedupe, rank

HASH_A = "a" * 40
HASH_B = "b" * 40


def make_result(**overrides) -> SearchResult:
    fields = dict(
        title="Ubuntu 26.04 LTS amd64", infohash=HASH_A, seeders=10, leechers=2,
        sources=[SourceHit(provider="x", seeders=10, leechers=2)],
    )
    fields.update(overrides)
    return SearchResult(**fields)


class TestModels:
    def test_normalize_infohash_lowercases_and_validates(self):
        assert normalize_infohash(HASH_A.upper()) == HASH_A
        assert normalize_infohash("nothex") is None
        assert normalize_infohash("") is None
        assert normalize_infohash(None) is None
        assert normalize_infohash("f" * 64) == "f" * 64  # v2 hashes accepted

    def test_infohash_from_magnet(self):
        assert infohash_from_magnet(f"magnet:?xt=urn:btih:{HASH_A.upper()}&dn=x") == HASH_A
        assert infohash_from_magnet("magnet:?dn=nohash") is None
        assert infohash_from_magnet("") is None

    def test_identity_prefers_hash_then_title(self):
        assert make_result().identity() == f"hash:{HASH_A}"
        assert make_result(infohash=None, title="Some  Title! v2").identity() == "title:some.title.v2"

    def test_ensure_magnet_synthesized_from_hash(self):
        assert make_result(magnet=None).ensure_magnet() == f"magnet:?xt=urn:btih:{HASH_A}"
        assert make_result(infohash=None, magnet=None).ensure_magnet() is None

    def test_query_validates_and_clamps(self):
        assert SearchQuery(text="  hello ").text == "hello"
        assert SearchQuery(text="x", limit=9999).limit == 200
        assert SearchQuery(text="x", limit=0).limit == 1
        with pytest.raises(ValueError):
            SearchQuery(text="   ")

    def test_to_dict_is_json_shaped(self):
        d = make_result(published=utcnow()).to_dict()
        assert d["infohash"] == HASH_A
        assert isinstance(d["published"], str)
        assert d["magnet"].startswith("magnet:?xt=urn:btih:")
        assert d["sources"][0]["provider"] == "x"


class TestDedupe:
    def test_merges_by_infohash_with_max_seeders(self):
        a = make_result(seeders=10, sources=[SourceHit(provider="p1", seeders=10)])
        b = make_result(seeders=25, sources=[SourceHit(provider="p2", seeders=25)])
        merged = dedupe([a, b])
        assert len(merged) == 1
        result = merged[0]
        assert result.seeders == 25  # max, not sum -- same swarm seen twice
        assert {hit.provider for hit in result.sources} == {"p1", "p2"}

    def test_fills_missing_fields_from_later_hits(self):
        a = make_result(magnet=None, size_bytes=None)
        b = make_result(magnet=f"magnet:?xt=urn:btih:{HASH_A}", size_bytes=1234)
        result = dedupe([a, b])[0]
        assert result.magnet is not None
        assert result.size_bytes == 1234

    def test_distinct_hashes_not_merged(self):
        assert len(dedupe([make_result(), make_result(infohash=HASH_B)])) == 2

    def test_input_results_not_mutated(self):
        a, b = make_result(), make_result(seeders=99)
        dedupe([a, b])
        assert len(a.sources) == 1


class TestRanking:
    def test_seeders_dominate(self):
        low, high = make_result(seeders=1), make_result(infohash=HASH_B, seeders=500)
        assert rank([low, high])[0] is high

    def test_recency_breaks_ties(self):
        old = make_result(published=utcnow() - timedelta(days=1500))
        new = make_result(infohash=HASH_B, published=utcnow() - timedelta(days=1))
        assert rank([old, new])[0] is new

    def test_tiny_size_penalized_as_junk(self):
        junk = make_result(size_bytes=512)
        real = make_result(infohash=HASH_B, size_bytes=2_000_000_000)
        assert rank([junk, real])[0] is real

    def test_multi_provider_corroboration_bonus(self):
        single = make_result()
        multi = make_result(infohash=HASH_B,
                            sources=[SourceHit(provider="p1"), SourceHit(provider="p2")])
        assert rank([single, multi])[0] is multi


class TestNaiveDateRanking:
    """A timezone-less published date (common from Jackett) must not crash the
    ranker. Before the fix, subtracting a naive date from an aware utcnow()
    raised TypeError *after* provider fan-out -- taking down the whole search,
    not just the one result."""

    def test_naive_datetime_does_not_crash_score(self):
        from datetime import datetime
        naive = datetime(2026, 7, 25, 10, 0, 0)  # no tzinfo
        assert naive.tzinfo is None
        result = make_result(published=naive)
        ranked = rank([result])  # must not raise
        assert len(ranked) == 1
        assert isinstance(ranked[0].score, float)

    def test_parser_normalizes_naive_to_utc(self):
        from adp.search.providers import _parse_iso_date
        parsed = _parse_iso_date("2026-07-25T10:00:00")  # no offset
        assert parsed is not None
        assert parsed.tzinfo is not None  # coerced to aware

    def test_mixed_naive_and_aware_results_rank_together(self):
        from datetime import datetime
        naive = make_result(published=datetime(2026, 1, 1, 0, 0, 0))
        aware = make_result(infohash=HASH_B, published=utcnow() - timedelta(days=2))
        ranked = rank([naive, aware])  # must not raise
        assert len(ranked) == 2
