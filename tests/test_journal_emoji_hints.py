from journal_emoji_hints import emoji_meaning_hints


def test_emoji_meaning_hints_empty_and_plain():
    assert emoji_meaning_hints("") == []
    assert emoji_meaning_hints("no symbols here") == []


def test_emoji_meaning_hints_decodes_common():
    hints = emoji_meaning_hints("Rough day 😰 but finished 🎉")
    assert "anxious face with sweat" in " ".join(hints).lower() or "face" in str(hints).lower()
    assert any("party" in h.lower() or "popper" in h.lower() or "tada" in h.lower() for h in hints)


def test_emoji_meaning_hints_dedupes():
    hints = emoji_meaning_hints("👍👍 great")
    assert hints
    assert len([h for h in hints if "thumb" in h.lower()]) <= 1
