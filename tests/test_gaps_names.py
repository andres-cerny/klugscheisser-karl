"""Group 7 of the chat evaluation: a name with a typo ("Lara Staldr") still finds the person, says so in the
scope note, and the stored data (not an old web page) answers."""

from sqlalchemy.orm import Session

from klugscheisser.answer import engine as eng
from klugscheisser.domain.matcher import AthleteMatcher
from klugscheisser.seed.loader import load_all_seeds


def test_typo_name_found_once_and_noted(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
        links = AthleteMatcher(s).from_text_fuzzy("How old is Lara Staldr?")
        assert [name for _link, name in links] == ["lara stalder"]
        athletes, note = eng.scope_athletes(s, "How old is Lara Staldr?", None, None, None)
        assert [a.full_name for a in athletes] == [
            "Lara Stalder"
        ] and note == "Name verstanden als Lara Stalder"
        # far from any name, or only a first name: nobody
        assert AthleteMatcher(s).from_text_fuzzy("Wie alt ist Maria Musterfrau?") == []
        assert AthleteMatcher(s).from_text_fuzzy("Was macht Lara?") == []
