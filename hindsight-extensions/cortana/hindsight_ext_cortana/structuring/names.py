"""Comparing names: case- and space-insensitive equality, and name variants ("Priya" and
"Priya Natarajan") whose keys are shown to the model beside a fact's subject. Subjects themselves resolve in code
(``validation``), never by these variants."""

import unicodedata

from slugify import slugify


def normalize_name(name: str) -> str:
    """A name compared case-insensitively with whitespace collapsed."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def name_words(name: str) -> frozenset[str]:
    return frozenset(slugify(name).split("-")) - {""}


def names_overlap(a: str, b: str) -> bool:
    """One name's words all appear in the other ("Priya" and "Priya Natarajan"): a likely variant of one
    subject, whose keys are shown to the model beside it. Only a prompt aid; subjects resolve in ``validation``."""
    wa, wb = name_words(a), name_words(b)
    return bool(wa) and bool(wb) and (wa <= wb or wb <= wa)
