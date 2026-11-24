"""Fact kinds (v6). German labels are used in answers; `value` keys per kind are listed for extractors."""

FACT_KINDS: dict[str, dict] = {
    "birth_date": {"de": "Geburtsdatum", "value": ["date"]},
    "birth_place": {"de": "Geburtsort", "value": ["text"]},
    "hometown": {"de": "Heimatort", "value": ["text"]},
    "height_cm": {"de": "Grösse", "value": ["number"]},
    "shoots": {"de": "Schusshand", "value": ["text"]},  # L | R
    "languages": {"de": "Sprachen", "value": ["text"]},
    "nickname": {"de": "Spitzname", "value": ["text"]},
    "position": {"de": "Position", "value": ["text"]},  # G | D | F or free text for other sports
    "jersey_number": {"de": "Rückennummer", "value": ["number", "team", "event"]},
    "college": {"de": "College/Hochschule", "value": ["text", "major", "years"]},
    "studies": {
        "de": "Studium",
        "value": ["text", "institution", "status"],
    },  # status: laufend | abgeschlossen
    "job": {"de": "Beruf", "value": ["text", "employer"]},
    "draft": {"de": "Draft", "value": ["league", "year", "round", "team"]},
    "national_team_event": {"de": "Turnier mit dem Nationalteam", "value": ["event", "year", "result"]},
    "appearances": {"de": "Einsätze", "value": ["number", "team"]},
    "national_team_debut": {"de": "Nationalteam-Debüt", "value": ["date", "text"]},
    # v11: Elite Prospects awards carry season, number ("89") and league
    "award": {"de": "Auszeichnung", "value": ["text", "year", "season", "number", "league"]},
    # v9: Olympic / world championship medal; metal: gold | silver | bronze
    "medal": {"de": "Medaille", "value": ["text", "metal", "year", "event"]},
    "milestone": {"de": "Meilenstein", "value": ["text", "date"]},
    "captaincy": {"de": "Captain", "value": ["team", "season", "role"]},
    "family_in_sport": {"de": "Familie im Sport", "value": ["text", "relation", "person"]},
    "engagement": {"de": "Engagement", "value": ["text"]},
    "sponsor": {"de": "Sponsor", "value": ["text"]},
    "injury": {"de": "Verletzung", "value": ["text", "date", "status"]},  # always Vorsicht
    "retirement": {"de": "Rücktritt", "value": ["date", "text"]},
    "comeback": {"de": "Comeback", "value": ["date", "text"]},
    # v11 (Elite Prospects)
    "weight_kg": {"de": "Gewicht", "value": ["number"]},
    "youth_team": {"de": "Juniorenklub", "value": ["text"]},
    "current_team": {"de": "Aktuelles Team", "value": ["team", "league", "season", "jersey", "status"]},
    "contract": {"de": "Vertrag bis", "value": ["text", "season_to"]},  # "28/29" → season_to 2028
    "agency": {"de": "Agentur", "value": ["text"]},
}
SENSITIVE_KINDS = {"injury", "family_in_sport"}
