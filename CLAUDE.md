# Klugscheisser Karl: notes for Claude

## Who Karl is for

Fabi, a Swiss TV sports presenter / journalist (Sportmoderatorin) covering Swiss women's ice hockey, mainly
via Perplexity, preparing broadcasts, commentary and interviews. Never call her a fan. She needs verified,
current, citable facts (date + source) and background on players of both teams; profiles/bios count for
background questions. Use this in every persona, reader context, eval rubric and agent brief.

> Architecture: read ARCHITECTURE.md before structural changes. If a change
> adds, removes, renames, or rewires a component, update ARCHITECTURE.md
> (diagram + table + "Last updated") in the same change. End every summary
> of a change with "Architecture impact: none" or one line saying what
> changed.
>
> The interactive architecture page (Claude Artifact) is built from
> ARCHITECTURE.md on origin/main by the `/arch-view` skill
> (`.claude/skills/arch-view/`). Say "update the arch graph" to refresh it.
> A free GitHub check (`.github/workflows/arch-check.yml`) warns on PRs that
> change structure without touching ARCHITECTURE.md; it never fails.
