"""Sport and gender words of the current project for prompts and answer texts (2026-11-24, floorball project).

Always called at use time (inside functions), never at import: a module constant would freeze the default
project's words. The defaults reproduce the ice hockey texts exactly ("Frauen-Eishockey", "Spielerinnen").
Prompt templates write the words as placeholders ({sport_topic}, {players} …) and go through `fill()` (plain
string replacement, so JSON braces in prompts stay as they are).
"""

from __future__ import annotations


def sport_topic_de() -> str:
    """'Frauen-Eishockey' (ice hockey project) / 'Männer-Unihockey' (floorball project)."""
    from klugscheisser.projects import wording
    from klugscheisser.sports import current_spec

    return f"{'Männer' if current_spec().men else 'Frauen'}-{wording().sport_de}"


def position_labels_de() -> dict[str, str]:
    """{"G": "Torhüterin", …} / {"G": "Torhüter", …} for the current project's sport."""
    from klugscheisser.sports import current_spec

    return current_spec().position_labels_de or {"G": "Torhüterin", "D": "Verteidigerin", "F": "Stürmerin"}


def players_dat() -> str:
    """Dative plural: "von 20 Spielerinnen" / "von 20 Spielern"."""
    from klugscheisser.projects import wording

    p = wording().players
    return p if p.endswith(("n", "s")) else p + "n"


def fill(text: str) -> str:
    """Replace the word placeholders of a prompt template with the current project's words."""
    from klugscheisser.projects import wording
    from klugscheisser.sports import current_spec

    w = wording()
    men = current_spec().men
    words = {
        "{sport_topic}": sport_topic_de(),
        "{sport_de}": w.sport_de,
        "{player}": w.player,
        "{players}": w.players,
        "{players_dat}": players_dat(),  # "von 20 Spielerinnen" / "von 20 Spielern"
        "{teammates}": w.teammates,
        "{pronoun}": w.pronoun_de,
        # "Schweizerinnen" / "Schweizer" (the nation's players)
        "{swiss_players}": "Schweizer" if men else "Schweizerinnen",
        "{swiss_players_dat}": "Schweizern" if men else "Schweizerinnen",
        "{no_player}": f"Kein {w.player}" if men else f"Keine {w.player}",  # "Keine Spielerin beider Teams …"
        "{a_player_dat}": f"einem {w.player}" if men else f"einer {w.player}",  # "frage mit einer Spielerin"
        "{a_player_acc}": f"einen {w.player}" if men else f"eine {w.player}",  # "Nenne … eine Spielerin."
    }
    for key, value in words.items():
        text = text.replace(key, value)
    return text
