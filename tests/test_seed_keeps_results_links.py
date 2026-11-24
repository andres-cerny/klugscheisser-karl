"""`seed base` again (nightly, after a deploy) must not undo what the results pipeline filled in later: the link of a
seeded tournament to the results' competition, its end date, a final status (2026-10-18: EHT Lahti/Södertälje lost
their competition link after a re-seed, so list_events could not group their games)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds


def test_reseed_keeps_values_the_seed_does_not_state(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
        ev = s.scalar(select(m.Event).where(m.Event.key == "eht-2026-lahti"))
        assert ev.competition_id is None and ev.ends_at is None  # the seed states neither
        sport = repo.get_sport(s, "icehockey_women")
        comp = m.Competition(sport_id=sport.id, key="eht-2026-27-lahti", name="EHT Lahti", kind="tournament")
        s.add(comp)
        s.flush()
        end = datetime(2026, 10, 3, 21, 59, tzinfo=UTC)
        ev.competition_id, ev.ends_at, ev.status = comp.id, end, "final"  # as the results pipeline does
        s.commit()
        load_all_seeds(s)
        s.commit()
        s.refresh(ev)
        assert (ev.competition_id, ev.ends_at, ev.status) == (comp.id, end, "final")
        assert ev.starts_at is not None and ev.city == "Lahti (FIN)"  # what the seed states is still set


def test_seed_values_win_where_the_seed_states_them(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
        ev = s.scalar(select(m.Event).where(m.Event.key == "eht-2026-lahti"))
        ev.city, ev.name = "Falsch", "Falscher Name"
        s.commit()
        load_all_seeds(s)
        s.commit()
        s.refresh(ev)
        assert ev.city == "Lahti (FIN)" and ev.name.startswith("Women's Euro Hockey Tour, Lahti")
        new = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        assert new.kind == "tournament" and new.status in ("scheduled", "live", "final")
