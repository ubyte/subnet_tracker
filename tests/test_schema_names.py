import re
import sqlite3

import pytest

from db_rows import ROWS, insert

PAIR_SHAPE = re.escape("pair name must differ from the canonical one by a leading www.")
ONE_PER_DOMAIN = re.escape("UNIQUE constraint failed: names.domain_id")


def add_domain(db: sqlite3.Connection, *names: str) -> int:
    domain_id = insert(db, "domains", ROWS["domains"])
    for i, name in enumerate(names):
        add_name(db, domain_id, name, canonical=i == 0)
    return domain_id


def add_name(db: sqlite3.Connection, domain_id: int, name: str, *, canonical: bool) -> int:
    return insert(db, "names", {"domain_id": domain_id, "name": name, "canonical": int(canonical)})


@pytest.mark.parametrize(("canonical", "pair"), [("example.com", "www.example.com"), ("www.example.com", "example.com"), ("api.example.com", "www.api.example.com")])
def test_pair_shape_accepts_www_pair_in_both_directions(db: sqlite3.Connection, canonical: str, pair: str) -> None:
    domain_id = add_domain(db, canonical)
    add_name(db, domain_id, pair, canonical=False)


@pytest.mark.parametrize("pair", ["api.example.com", "www.www.example.com", "wwwexample.com", "ww.example.com", "www.example.org", "www.", "w.example.com"])
def test_pair_shape_rejects_other_names(db: sqlite3.Connection, pair: str) -> None:
    domain_id = add_domain(db, "example.com")
    with pytest.raises(sqlite3.IntegrityError, match=PAIR_SHAPE):
        add_name(db, domain_id, pair, canonical=False)


def test_pair_shape_rejects_pair_without_canonical(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=PAIR_SHAPE):
        add_name(db, add_domain(db), "www.example.com", canonical=False)


def test_pair_shape_rejects_pair_of_another_domains_canonical(db: sqlite3.Connection) -> None:
    add_domain(db, "example.com")
    with pytest.raises(sqlite3.IntegrityError, match=PAIR_SHAPE):
        add_name(db, add_domain(db, "example.org"), "www.example.com", canonical=False)


def test_one_canonical_name_per_domain(db: sqlite3.Connection) -> None:
    domain_id = add_domain(db, "example.com")
    with pytest.raises(sqlite3.IntegrityError, match=ONE_PER_DOMAIN):
        add_name(db, domain_id, "example.org", canonical=True)


def test_one_pair_name_per_domain(db: sqlite3.Connection) -> None:
    domain_id = add_domain(db, "www.example.com", "example.com")
    with pytest.raises(sqlite3.IntegrityError, match=ONE_PER_DOMAIN):
        add_name(db, domain_id, "www.www.example.com", canonical=False)


def test_each_domain_has_its_own_canonical_and_pair(db: sqlite3.Connection) -> None:
    add_domain(db, "example.com", "www.example.com")
    add_domain(db, "www.example.org", "example.org")
    assert [tuple(row) for row in db.execute("SELECT canonical, count(*) FROM names GROUP BY canonical")] == [(0, 2), (1, 2)]


def test_name_belongs_to_one_domain(db: sqlite3.Connection) -> None:
    add_domain(db, "example.com")
    with pytest.raises(sqlite3.IntegrityError, match=re.escape("UNIQUE constraint failed: names.name")):
        add_domain(db, "example.com")
