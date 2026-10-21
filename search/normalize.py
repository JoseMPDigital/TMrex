"""Mark-name normalization used for name matching (docs/protocol.md, Section 5).

Committed with the protocol, before any scoring, so the matching rule is fixed in code.
The scoring script imports normalize() and applies it to both the model's mark names and
the register's mark text.

Rule: Unicode-normalize (NFKC), ignore case, treat "&" and "and" as the same, and ignore all
punctuation and spaces.

>>> normalize("Apex & Co.")
'apexandco'
>>> normalize("APEX AND CO")
'apexandco'
>>> normalize("apex-and-co!")
'apexandco'
>>> normalize("  Café  Ō ")
'caféō'
>>> normalize("") == normalize("   ")
True
"""
import unicodedata


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").casefold()
    s = s.replace("&", "and")
    return "".join(ch for ch in s if ch.isalnum())


if __name__ == "__main__":
    import doctest
    failures, _ = doctest.testmod()
    print("normalize: all examples pass" if not failures else f"normalize: {failures} failure(s)")
