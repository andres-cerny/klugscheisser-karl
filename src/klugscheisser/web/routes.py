"""Web app routes: search, person/team/event pages, question box, refresh. German UI, server-rendered + htmx."""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from klugscheisser import translate as tr
from klugscheisser.adds.flow import parse_add_message
from klugscheisser.chat import service as chat_svc
from klugscheisser.config import Settings, get_settings
from klugscheisser.db import models as m
from klugscheisser.db.session import session_scope
from klugscheisser.interfaces import AnswerEngine
from klugscheisser.mcp_server import tools as t
from klugscheisser.projects import wording
from klugscheisser.sources_registry import person_sources
from klugscheisser.sports import home_team_key
from klugscheisser.web import access, views
from klugscheisser.web import card as sb
from klugscheisser.web import card_data as cd
from klugscheisser.web import data as d
from klugscheisser.web import person as pp
from klugscheisser.web import photo as wphoto
from klugscheisser.web import roster as rs
from klugscheisser.web import statistik as st
from klugscheisser.web import timeline as tl
from klugscheisser.web.bots import is_bot_request
from klugscheisser.web.html import e, layout, poll_fragment, safe_url

log = logging.getLogger("klugscheisser.web")


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def page(
    title: str,
    body: str,
    status: int = 200,
    nav: str = "",
    wide: bool = False,
    search: bool = True,
    menu: str = "",
    bare: bool = False,
) -> HTMLResponse:
    return HTMLResponse(layout(title, body, nav, wide, search, menu, bare), status_code=status)


def not_found_body(text: str) -> str:
    """ui-loop round 3: what is missing and the two ways on (search / chat)."""
    return (
        f"<div class='gate'><h1>Nicht gefunden</h1><p class='meta'>{e(text)}</p>"
        "<div class='acts'><a class='btn primary' href='/suche'>Zur Suche</a>"
        "<a class='btn' href='/'>Zur Startseite</a></div></div>"
    )


def not_found(text: str, bare: bool = False) -> HTMLResponse:
    return page("Nicht gefunden", not_found_body(text), 404, bare=bare)


# paths of the MCP app the web app is mounted into: their 404 stays Starlette's plain one
PLAIN_404 = ("/mcp", "/.well-known/", "/healthz")


def unknown_path(request: Request) -> Response:
    """Last route: a GET to an address nobody knows gets the web 404 page (logo only, no data, no login
    needed); MCP-side paths keep the plain 'Not Found'."""
    path = request.url.path
    if any(path == p or path.startswith(p.rstrip("/") + "/") for p in PLAIN_404):
        return PlainTextResponse("Not Found", status_code=404)
    return not_found("Diese Adresse gibt es nicht (mehr). Vielleicht ein alter Link?", bare=True)


# --- routes --------------------------------------------------------------------------------------------

_answering: set[int] = set()  # chat ids with an answer in progress (one answer at a time per chat)
_answering_lock = threading.Lock()


def web_routes(settings: Settings | None = None) -> list[Route]:
    """Routes for the web app. Mount next to /mcp and /healthz (no path clash)."""
    settings = settings or get_settings()
    engine_box: dict[str, AnswerEngine] = {}

    def engine() -> AnswerEngine:
        if "e" not in engine_box:
            from klugscheisser.web.engine import get_answer_engine

            engine_box["e"] = get_answer_engine(settings)
        return engine_box["e"]

    def guarded(fn: Callable[[Request], Any]) -> Callable[[Request], Any]:
        async def wrapper(request: Request) -> Response:
            verdict = access.decide(settings, request)
            if verdict == "login":
                if is_htmx(request):
                    return Response(status_code=401, headers={"HX-Redirect": "/login"})
                return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
            if verdict != "ok":
                return page(
                    "Kein Zugang",
                    "<h1>Kein Zugang</h1><p>Diese Seite ist privat. Bitte mit der freigegebenen E-Mail-Adresse "
                    "über Cloudflare Access anmelden.</p>",
                    403,
                )
            if request.method == "POST" and not access.same_origin(request):
                return page("Abgelehnt", "<h1>Abgelehnt</h1><p>Fremde Herkunft (Origin).</p>", 403)
            if inspect.iscoroutinefunction(fn):
                return await fn(request)
            return await run_in_threadpool(fn, request)  # DB work and slow calls off the event loop

        wrapper.__name__ = fn.__name__
        return wrapper

    async def form_of(request: Request) -> dict[str, str]:
        form = await request.form()
        return {k: str(v) for k, v in form.items()}

    def log_from_search(s, request: Request, ref: str, label: str) -> None:
        if q := request.query_params.get("q"):
            chat_svc.log_search(s, q, ref, label)

    # --- pages ---------------------------------------------------------------------------------------

    def home(request: Request) -> Response:
        with session_scope() as s:
            body = views.home_body(
                chat_svc.recent_searches(s), chat_svc.recent_chats(s), d.upcoming_events(s)[:4]
            )
            return page("Suche", body, nav="suche", search=False)

    def search(request: Request) -> Response:
        q = request.query_params.get("q", "")
        with session_scope() as s:
            res = d.suggest(s, q)
            if is_htmx(request):
                return HTMLResponse(views.suggest_html(res, q))
            if q.strip():
                chat_svc.log_search(s, q)
                body = views.search_page(res, q)
            else:
                body = views.search_page(res, q, chat_svc.recent_searches(s))
        return page(f"Suche: {q.strip()}" if q.strip() else "Suche", body, nav="suche", search=False)

    def person(request: Request) -> Response:
        from klugscheisser.facts import funfacts as ff

        athlete_id = request.path_params["id"]
        tab = "chat" if request.query_params.get("tab") == "chat" else "info"
        with session_scope() as s:
            a = s.get(m.Athlete, athlete_id)
            if a is None:
                return not_found(f"Keine Person mit der Nummer {athlete_id}.")
            log_from_search(s, request, f"person:{a.id}", a.full_name)
            update = None
            if settings.web_refresh_for_bots or not is_bot_request(
                request
            ):  # bots: no paid update (web/bots.py)
                update = d.start_background_update(s, settings, [a])
            p = d.person_data(s, settings, a)
            card = cd.card_data(s, a)
            stats = st.stats_data(s, a)
            facts = ff.fun_facts(s, a.id)
            funfacts = sb.fun_facts_html(
                a.id, facts, ff.generated_at(s, a.id), tr.for_facts(s, settings, facts)
            )
            chat = None
            if not request.query_params.get("neu"):
                chats = chat_svc.recent_chats(s, limit=1, athlete_id=a.id)
                chat = chats[0] if chats else None
            ctx = {"person": a.id, "label": a.full_name}
            panel = views.chat_panel(
                chat, settings, ctx, embedded=True, questions=tl.person_questions(p, facts)
            )
            body = pp.person_page(
                p,
                update,
                card,
                funfacts,
                len(facts),
                cd.achievements(s, a),
                panel,
                views.person_sources_box(person_sources(s, a)),
                tab,
                has_chat=chat is not None,
                statistik=st.statistik_html(stats),
                n_seasons=stats["n_seasons"] if stats else 0,
                stats=stats,
            )
            menu = "<a href='#daten' onclick=\"document.getElementById('daten').open=true\">Daten zu dieser Person</a><hr>"
            return page(a.full_name, body, wide=True, menu=menu)

    def person_funfacts(request: Request) -> Response:
        from klugscheisser.facts import funfacts as ff
        from klugscheisser.web import card as sb

        with session_scope() as s:
            a = s.get(m.Athlete, request.path_params["id"])
            if a is None:
                return not_found("Keine Person mit dieser Nummer.")
            try:
                ff.generate(s, settings, a)
            except Exception as exc:
                log.exception("fun facts failed")
                return HTMLResponse(
                    f"<section id='funfacts'><p class='err'>Fun Facts fehlgeschlagen: {e(exc)}</p></section>"
                )
            facts = ff.fun_facts(s, a.id)
            return HTMLResponse(
                sb.fun_facts_html(a.id, facts, ff.generated_at(s, a.id), tr.for_facts(s, settings, facts))
            )

    async def person_tier(request: Request) -> Response:
        from klugscheisser import tiers

        form = await request.form()
        raw = str(form.get("tier") or "auto")
        with session_scope() as s:
            a = s.get(m.Athlete, request.path_params["id"])
            if a is None:
                return not_found("Keine Person mit dieser Nummer.")
            tiers.set_manual(s, a, int(raw) if raw.isdigit() else None)
        return RedirectResponse(f"/person/{request.path_params['id']}", status_code=303)

    def person_posts(request: Request) -> Response:
        athlete_id = request.path_params["id"]
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0
        with session_scope() as s:
            a = s.get(m.Athlete, athlete_id)
            if a is None:
                return not_found(f"Keine Person mit der Nummer {athlete_id}.")
            res = d.instagram_page(s, settings, a.id, offset)
        html = "".join(tl.row_html(tl.item_entry(i)) for i in res["posts"]) + pp.more_button(
            athlete_id, res["mehr"]
        )
        if is_htmx(request):
            return HTMLResponse(html)
        return page(
            a.full_name,
            f"<div class='results'><h1><a href='/person/{a.id}'>{e(a.full_name)}</a></h1><h2>Instagram</h2>{html}</div>",
        )

    def pronunciation_info(request: Request) -> Response:
        from klugscheisser import pronounce

        with session_scope() as s:
            a = s.get(m.Athlete, request.path_params["id"])
            if a is None:
                return HTMLResponse("")
            row = pronounce.info(s, settings, a)
            bits = []
            # v11: Elite Prospects' recording of her name (mp3), linked to her EP page
            label, url = row.label, row.source_url
            if row.source == "eliteprospects":
                label = "Aufnahme (Elite Prospects)"
                if not url and a.eliteprospects_id:
                    url = f"https://www.eliteprospects.com/player/{a.eliteprospects_id}"
            src = (
                f"<a href='{safe_url(url)}' target='_blank' rel='noopener'>{e(label)}</a>"
                if url
                else e(label)
            )
            if row.hint:
                bits.append(
                    f"Aussprache: <strong>{e(row.hint)}</strong> <span>(automatisch erzeugt, ohne Gewähr)</span>"
                )
            if row.ipa:
                ipa_src = (
                    f" <a href='{safe_url(row.ipa_source_url)}' target='_blank' rel='noopener'>Quelle</a>"
                    if row.ipa_source_url
                    else ""
                )
                bits.append(f"IPA [{e(row.ipa)}]{ipa_src}")
            bits.append(f"Ton: {src}")
            return HTMLResponse(" · ".join(bits))

    def pronunciation_audio(request: Request) -> Response:
        from klugscheisser import pronounce

        with session_scope() as s:
            a = s.get(m.Athlete, request.path_params["id"])
            if a is None:
                return Response(status_code=404)
            try:
                data, mime = pronounce.audio(s, settings, a)
            except pronounce.Unavailable as exc:
                return Response(str(exc), status_code=503, media_type="text/plain; charset=utf-8")
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})

    def person_photo(request: Request) -> Response:
        """Steckbrief photo, loaded by htmx after the page (Wikimedia lookup ≤ 5 s; nothing on failure)."""
        with session_scope() as s:
            a = s.get(m.Athlete, request.path_params["id"])
            meta = wphoto.ensure_photo(s, a) if a is not None else None
            return HTMLResponse(sb.photo_html(meta, a.full_name) if a is not None else "")

    def side_chat(s, ctx: dict[str, Any], request: Request, **where) -> str:
        """The team / event chat next to the roster: the latest one about it, or a new one (?neu=1)."""
        chat = None
        if not request.query_params.get("neu"):
            chats = chat_svc.recent_chats(s, limit=1, **where)
            chat = chats[0] if chats else None
        panel = views.chat_panel(chat, settings, ctx, embedded=True, questions=tl.TEAM_QUESTIONS)
        new = f"{request.url.path}?tab=chat&neu=1" if chat else None
        return pp.chat_aside("Chat", f"zu {ctx['label']}", panel, new)

    def event(request: Request) -> Response:
        key = request.path_params["key"]
        tab = "chat" if request.query_params.get("tab") == "chat" else "kader"
        with session_scope() as s:
            ev = s.scalar(select(m.Event).where(m.Event.key == key))
            if ev is None:
                try:
                    ev = t.resolve_event(s, key)
                except t.NotFound:
                    return not_found(f"Kein Anlass '{key}'.")
            log_from_search(s, request, f"event:{ev.key}", ev.name)
            data = d.event_data(s, settings, ev)
            ctx = {"event": ev.key, "label": ev.name}
            aside = side_chat(s, ctx, request, event_id=ev.id)
            return page(ev.name, rs.event_body(data, aside, tab), wide=True)

    def team(request: Request) -> Response:
        tab = "chat" if request.query_params.get("tab") == "chat" else "kader"
        with session_scope() as s:
            tm = s.get(m.Team, request.path_params["id"])
            if tm is None:
                return not_found("Kein Team mit dieser Nummer.")
            log_from_search(s, request, f"team:{tm.id}", tm.name)
            data = d.team_data(s, settings, tm)
            aside = side_chat(s, {"team": tm.id, "label": tm.name}, request, team_id=tm.id)
            return page(tm.name, rs.team_body(data, aside, tab), wide=True)

    def brief(request: Request) -> Response:
        from klugscheisser.web import brief as b

        qp = request.query_params
        iocs = [x.upper() for x in (qp.get("a", ""), qp.get("b", "")) if x]
        with session_scope() as s:
            ev = (
                s.scalar(select(m.Event).where(m.Event.key == qp.get("event", "")))
                if qp.get("event")
                else None
            )
            if ev is None:  # newest tournament with both teams
                ev = s.scalar(
                    select(m.Event)
                    .join(m.EventTeam)
                    .join(m.Team)
                    .where(m.Event.kind != "game", m.Team.country.in_(iocs))
                    .order_by(m.Event.starts_at.desc().nulls_last())
                )
            if len(iocs) != 2 or iocs[0] == iocs[1]:
                return page("Spiel-Brief", "<h1>Spiel-Brief</h1><p>Bitte zwei verschiedene Teams wählen.</p>")
            return page("Spiel-Brief", b.brief_html(b.brief_data(s, iocs, ev)), wide=False)

    def sources(request: Request) -> Response:
        from klugscheisser.sources_registry import general_sources

        return page("Quellen", views.sources_page(general_sources(settings)))

    # --- chat ----------------------------------------------------------------------------------------

    def _context(qp: dict[str, Any]) -> dict[str, Any]:
        """person/team/event ids from a form or query string, checked against the DB."""
        ctx: dict[str, Any] = {}
        with session_scope() as s:
            person_id, team_id, event_key = qp.get("person"), qp.get("team"), qp.get("event")
            if person_id and str(person_id).isdigit() and (a := s.get(m.Athlete, int(person_id))):
                ctx.update(person=a.id, label=a.full_name)
            elif team_id and str(team_id).isdigit() and (tm := s.get(m.Team, int(team_id))):
                ctx.update(team=tm.id, label=tm.name)
            elif event_key and (ev := s.scalar(select(m.Event).where(m.Event.key == str(event_key)))):
                ctx.update(event=ev.key, event_id=ev.id, label=ev.name)
        return ctx

    def chat_new(request: Request) -> Response:
        ctx = _context(dict(request.query_params))
        with session_scope() as s:
            if ctx:
                head = (
                    "<h1 id='chattitle'>Neuer Chat</h1>"
                    f"<div class='small'>Bezogen auf: <strong>{e(ctx['label'])}</strong></div>"
                )
                body = f"<div class='results chatpage'>{head}{views.chat_panel(None, settings, ctx)}</div>"
                return page("Chat", body, nav="chat")
            return page("Chat", views.chat_page(None, settings, chat_svc.recent_chats(s)), nav="chat")

    async def chat_create(request: Request) -> Response:
        form = await form_of(request)
        form["vorschlag"] = "\n".join(str(x) for x in (await request.form()).getlist("vorschlag"))
        return await run_in_threadpool(_chat_create, request, form)

    def _chat_create(request: Request, form: dict[str, str]) -> Response:
        question = form.get("frage", "").strip()
        if not question:
            return HTMLResponse("<p class='err'>Bitte eine Frage eingeben.</p>", status_code=400)
        ctx = _context(form)
        with session_scope() as s:
            chat = chat_svc.create_chat(
                s,
                question,
                athlete_id=ctx.get("person"),
                team_id=ctx.get("team"),
                event_id=ctx.get("event_id"),
            )
            chat_svc.add_question(s, chat, question)
            s.flush()
            chat_id = chat.id
            if is_htmx(request):
                s.refresh(chat)
                questions = [x for x in form.get("vorschlag", "").split("\n") if x.strip()]
                html = views.chat_panel(
                    chat, settings, ctx, embedded=bool(form.get("eingebettet")), questions=questions
                )
                headers = {}
                if not form.get("eingebettet"):  # own chat page: new URL, title, no list of other chats
                    headers = {"HX-Push-Url": f"/chat/{chat_id}"}
                    html += (
                        f"<h1 id='chattitle' hx-swap-oob='true'>{e(chat.title)}</h1>"
                        "<section id='recentchats' hx-swap-oob='delete'></section>"
                    )
                return HTMLResponse(html, headers=headers)
        return RedirectResponse(f"/chat/{chat_id}", status_code=303)

    def chat_view(request: Request) -> Response:
        with session_scope() as s:
            chat = s.get(m.Chat, request.path_params["id"])
            if chat is None:
                return not_found("Kein Chat mit dieser Nummer.")
            return page(chat.title, views.chat_page(chat, settings, []), nav="chat")

    def chats_all(request: Request) -> Response:
        with session_scope() as s:
            return page("Chats", views.chats_list(chat_svc.recent_chats(s, limit=200)), nav="chat")

    async def chat_message(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(_chat_message, request, form)

    def _chat_message(request: Request, form: dict[str, str]) -> Response:
        question = form.get("frage", "").strip()
        with session_scope() as s:
            chat = s.get(m.Chat, request.path_params["id"])
            if chat is None:
                return not_found("Kein Chat mit dieser Nummer.")
            if not question:
                return HTMLResponse("")
            chat_svc.add_question(s, chat, question)
            if is_htmx(request):
                fresh = bool(form.get("frisch"))
                return HTMLResponse(views.user_bubble(question) + views.pending_answer(chat.id, fresh=fresh))
            return RedirectResponse(f"/chat/{chat.id}", status_code=303)

    def chat_answer(request: Request) -> Response:
        chat_id = request.path_params["id"]
        with _answering_lock:
            busy = chat_id in _answering
            if not busy:
                _answering.add(chat_id)
        if busy:
            return HTMLResponse(views.pending_answer(chat_id, delay="3s"))
        try:
            return HTMLResponse(_answer_chat(chat_id, fresh=bool(request.query_params.get("frisch"))))
        finally:
            with _answering_lock:
                _answering.discard(chat_id)

    def _answer_chat(chat_id: int, fresh: bool = False) -> str:
        with session_scope() as s:
            chat = s.get(m.Chat, chat_id)
            if chat is None or not chat.messages:
                return ""
            last = chat.messages[-1]
            if last.role != "user":
                return views.bot_message(last, settings, s)  # already answered (e.g. second tab)
            question = last.text
            past = chat_svc.history(chat, settings.chat_history_turns)
            scope = chat_svc.scope_for(s, chat, question, past)
            style = chat_svc.answer_style(s, settings)  # v12: prosa | liste (project setting)
        if add := parse_add_message(question):
            return _add_from_chat(chat_id, *add)
        if brief_link := _brief_link(question):
            with session_scope() as s:
                msg = chat_svc.add_note(s, s.get(m.Chat, chat_id), f"Spiel-Brief: {brief_link}")
                return (
                    f"<div class='msg bot'><p><a href='{e(brief_link)}'>Spiel-Brief öffnen</a> "
                    "(Kader, Verbindungen zwischen den Teams, aktuelle Meldungen; jede Angabe mit Quelle).</p></div>"
                )
        log.info("chat %s frage %r scope=%s", chat_id, question[:120], scope)
        try:
            res = engine().answer(
                question,
                person_ids=scope.get("person_ids") or None,
                team_id=scope.get("team_id"),
                event_id=scope.get("event_id"),
                history=past or None,
                **({"use_cache": False} if fresh else {}),
                **({"style": style} if style != "liste" else {}),
            )
        except Exception as exc:  # show the error instead of a blank box
            log.exception("answer failed")
            with session_scope() as s:
                chat_svc.add_note(s, s.get(m.Chat, chat_id), f"Fehler bei der Antwort: {exc}")
            return f"<div class='msg bot'><p class='err'>Fehler bei der Antwort: {e(exc)}</p></div>"
        with session_scope() as s:
            msg = chat_svc.add_answer(s, s.get(m.Chat, chat_id), res)
            return views.bot_message(msg, settings, s)

    def _brief_link(question: str) -> str | None:
        import re as _re

        from klugscheisser.adds.wiki import countries_in
        from klugscheisser.web.brief import brief_url

        if not _re.search(
            r"\b(brief|briefing|vorbereitung auf das spiel|matchvorbereitung)\b", question, _re.I
        ):
            return None
        from klugscheisser.adds.wiki import _COUNTRY_RE

        iocs = sorted(
            countries_in(question), key=lambda c: _COUNTRY_RE[c].search(question).start()
        )  # text order
        return brief_url(iocs[:2], None) if len(iocs) >= 2 else None

    def _add_from_chat(chat_id: int, kind: str, what: str) -> str:
        from klugscheisser.adds import flow

        inp = {"request": what} if kind == "team" else {"name": what, "details": ""}
        with session_scope() as s:
            try:
                req = flow.propose(s, settings, kind, inp)
            except Exception as exc:
                log.exception("add proposal failed")
                chat_svc.add_note(s, s.get(m.Chat, chat_id), f"Vorschlag fehlgeschlagen: {exc}")
                return f"<div class='msg bot'><p class='err'>Vorschlag fehlgeschlagen: {e(exc)}</p></div>"
            msg = chat_svc.add_note(s, s.get(m.Chat, chat_id), req.proposal.get("summary", ""), req.id)
            return views.bot_message(msg, settings, s)

    # --- ratings (v6) -------------------------------------------------------------------------------------

    async def rate(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(_rate, request, form)

    def _rate(request: Request, form: dict[str, str]) -> Response:
        from datetime import UTC, datetime

        with session_scope() as s:
            msg = s.get(m.ChatMessage, request.path_params["id"])
            if msg is None or msg.answer is None:
                return not_found("Keine Antwort mit dieser Nummer.")
            if form.get("rating") in {"gut", "okay", "schlecht"}:
                msg.rating = form["rating"]
            if "grund" in form:
                msg.rating_reason = form["grund"].strip()[:2000] or None
            msg.rated_at = datetime.now(UTC)
            s.flush()
            log.info("rating msg=%s %s %r", msg.id, msg.rating, (msg.rating_reason or "")[:80])
            if is_htmx(request):
                return HTMLResponse(views.rating_widget(msg))
            return RedirectResponse(f"/chat/{msg.chat_id}", status_code=303)

    def _rated_rows(s, limit: int = 300):
        msgs = list(
            s.scalars(
                select(m.ChatMessage)
                .where(m.ChatMessage.role == "assistant", m.ChatMessage.answer.is_not(None))
                .order_by(m.ChatMessage.id.desc())
                .limit(limit)
            )
        )
        rows = []
        for msg in msgs:
            q = s.scalar(
                select(m.ChatMessage.text)
                .where(
                    m.ChatMessage.chat_id == msg.chat_id,
                    m.ChatMessage.role == "user",
                    m.ChatMessage.id < msg.id,
                )
                .order_by(m.ChatMessage.id.desc())
                .limit(1)
            )
            rows.append((msg, q or ""))
        return rows

    def ratings(request: Request) -> Response:
        with session_scope() as s:
            rows = _rated_rows(s)
            counts: dict[str, int] = {}
            for msg, _ in rows:
                counts[msg.rating or ""] = counts.get(msg.rating or "", 0) + 1
            return page("Bewertungen", views.ratings_page(rows, counts))

    def ratings_json(request: Request) -> Response:
        from starlette.responses import JSONResponse

        from klugscheisser import projects

        with session_scope() as s:
            data = [
                {
                    "projekt": projects.current().key,
                    "frage": q,
                    "bewertung": msg.rating,
                    "briefing_version": msg.briefing_id,
                    "grund": msg.rating_reason,
                    "zeit": msg.created_at.isoformat(),
                    "chat": msg.chat_id,
                    "antwort": msg.answer,
                }
                for msg, q in _rated_rows(s, limit=2000)
            ]
        return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=bewertungen.json"})

    # --- v7: project page (briefing, learned part, settings) ------------------------------------------

    def project_view(request: Request, saved: str = "", errors: list[str] | None = None) -> Response:
        from klugscheisser.web.project_page import page_body

        with session_scope() as s:
            body = page_body(s, request.headers.get("host", ""), saved, errors)
        return page("Projekt", body)

    def project_get(request: Request) -> Response:
        return project_view(request, request.query_params.get("ok", ""))

    def _done(text: str) -> Response:
        return RedirectResponse(f"/projekt?ok={quote(text)}", status_code=303)

    async def project_post(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(_project_post, request, form)

    def _project_post(request: Request, form: dict[str, str]) -> Response:
        from klugscheisser import briefing, projects
        from klugscheisser.web.project_page import save_settings

        what = request.path_params["what"]
        with session_scope() as s:
            if what == "briefing":
                briefing.save(s, text=form.get("text", ""), note="Text geändert")
                return _done("Briefing gespeichert (gilt ab der nächsten Antwort).")
            if what == "gelernt":
                briefing.save(s, learned=form.get("learned", ""), note="Gelerntes bearbeitet")
                return _done("Gelerntes gespeichert.")
            if what == "einstellungen":
                errors = save_settings(s, projects.current(), form)
                if errors:
                    s.rollback()
                    return project_view(request, errors=errors)
                return _done("Einstellungen gespeichert.")
            if what == "vorschlag":
                sug = briefing.suggest(s, settings)
                if sug is None:
                    return _done("Noch zu wenig Fragen und Bewertungen für einen Vorschlag.")
                return _done("Neuer Vorschlag unten – übernehmen oder ablehnen.")
        return not_found("Unbekannte Aktion")

    async def suggestion_post(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(_suggestion_post, request, form)

    def _suggestion_post(request: Request, form: dict[str, str]) -> Response:
        from klugscheisser import briefing

        with session_scope() as s:
            sug = s.get(m.BriefingSuggestion, request.path_params["id"])
            if sug is None or sug.status != "offen":
                return not_found("Vorschlag nicht gefunden")
            action = form.get("aktion", "annehmen")
            text = form.get("text")
            edited = text if text is not None and text.strip() != sug.text.strip() else None
            briefing.decide(s, sug, "ablehnen" if action == "ablehnen" else "annehmen", edited)
        return _done("Vorschlag abgelehnt." if action == "ablehnen" else "Ins Briefing übernommen.")

    # --- add persons / teams -------------------------------------------------------------------------

    async def add_create(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(_add_create, request, form)

    def _add_create(request: Request, form: dict[str, str]) -> Response:
        from klugscheisser.adds import flow

        kind = "team" if form.get("kind") == "team" else "person"
        if kind == "team":
            inp = {"request": form.get("request", "").strip()}
            missing = not inp["request"]
        else:
            inp = {
                "name": form.get("name", "").strip(),
                "instagram": form.get("instagram", "").strip() or None,
                "details": form.get("details", "").strip(),
            }
            missing = not inp["name"]
        if missing:
            return HTMLResponse("<p class='err'>Bitte Namen bzw. Team angeben.</p>")
        with session_scope() as s:
            try:
                req = flow.propose(s, settings, kind, inp)
            except Exception as exc:
                log.exception("add proposal failed")
                return HTMLResponse(f"<p class='err'>Vorschlag fehlgeschlagen: {e(exc)}</p>")
            html = views.proposal_card(req, settings, s)
            if is_htmx(request):
                return HTMLResponse(html)
            return page("Hinzufügen", f"<h1>Hinzufügen</h1>{html}")

    def add_view(request: Request) -> Response:
        with session_scope() as s:
            req = s.get(m.AddRequest, request.path_params["id"])
            if req is None:
                return not_found("Keine Anfrage mit dieser Nummer.")
            html = views.proposal_card(req, settings, s)
            if is_htmx(request):
                return HTMLResponse(html)
            return page("Hinzufügen", f"<h1>Hinzufügen</h1>{html}")

    def add_yes(request: Request) -> Response:
        from klugscheisser.adds import flow  # noqa: F401  (resolvers registered)
        from klugscheisser.adds.load import confirm

        with session_scope() as s:
            req = s.get(m.AddRequest, request.path_params["id"])
            if req is None:
                return not_found("Keine Anfrage mit dieser Nummer.")
            if req.status == "proposed" and (req.proposal or {}).get("found"):
                try:
                    if req.kind == "event":
                        from klugscheisser.adds.event import confirm_event

                        confirm_event(s, settings, req)
                    else:
                        confirm(s, settings, req)
                except Exception as exc:
                    log.exception("add confirm failed")
                    s.rollback()
                    return HTMLResponse(f"<p class='err'>Hinzufügen fehlgeschlagen: {e(exc)}</p>")
            s.refresh(req)
            return HTMLResponse(views.proposal_card(req, settings, s))

    def add_no(request: Request) -> Response:
        from klugscheisser.adds import flow

        with session_scope() as s:
            req = s.get(m.AddRequest, request.path_params["id"])
            if req is None:
                return not_found("Keine Anfrage mit dieser Nummer.")
            flow.reject(s, req)
            return HTMLResponse(views.proposal_card(req, settings, s))

    async def prep_create(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(_prep_create, request, form)

    def _prep_create(request: Request, form: dict[str, str]) -> Response:
        from klugscheisser.adds.event import propose_event

        with session_scope() as s:
            ev = s.scalar(select(m.Event).where(m.Event.key == form.get("event", "")))
            if ev is None:
                return HTMLResponse("<p class='err'>Kein Anlass.</p>")
            team_ids = None
            if form.get("teams") == "schweiz":
                team_ids = [t.id for t in s.scalars(select(m.Team).where(m.Team.key == home_team_key()))]
            req = propose_event(s, settings, ev, team_ids)
            return HTMLResponse(views.proposal_card(req, settings, s))

    async def add_refine(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(_add_refine, request, form)

    def _add_refine(request: Request, form: dict[str, str]) -> Response:
        from klugscheisser.adds import flow

        text = form.get("text", "").strip()
        with session_scope() as s:
            req = s.get(m.AddRequest, request.path_params["id"])
            if req is None:
                return not_found("Keine Anfrage mit dieser Nummer.")
            if not text or req.status != "proposed":
                return HTMLResponse(views.proposal_card(req, settings, s))
            try:
                new = flow.refine(s, settings, req, text)
            except Exception as exc:
                log.exception("add refine failed")
                return HTMLResponse(f"<p class='err'>Neuer Vorschlag fehlgeschlagen: {e(exc)}</p>")
            # the new card replaces the old one in place (same element id is not needed: outerHTML swap)
            return HTMLResponse(views.proposal_card(new, settings, s))

    # --- refresh (unchanged) -------------------------------------------------------------------------

    async def refresh_post(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(refresh_page, request, form.get("target", "").strip())

    async def deep_post(request: Request) -> Response:
        form = await form_of(request)
        return await run_in_threadpool(deep_page, request, form.get("target", "").strip())

    def deep_page(request: Request, target: str) -> Response:
        from klugscheisser import deep, tiers

        kind, _, ref = target.partition(":")
        with session_scope() as s:
            if kind == "person" and ref.isdigit():
                ids = [int(ref)]
            elif kind == "event":
                ev = s.scalar(select(m.Event).where(m.Event.key == ref))
                ids = (
                    [
                        x.athlete_id
                        for x in s.scalars(select(m.EventEntry).where(m.EventEntry.event_id == ev.id))
                    ]
                    if ev
                    else []
                )
            else:
                ids = []
            ids = [i for i in ids if tiers.effective(s.get(m.Athlete, i)) in (1, 2)]
            if not ids:
                html = "<div class='small'>Nur für Tier 1 und Tier 2 – hier nichts zu laden.</div>"
            else:
                cap = sum(deep.PER_PERSON_CAP[tiers.effective(s.get(m.Athlete, i))] for i in ids)
                job = deep.start_job(s, settings, ids)
                html = (
                    f"<div class='small'>Lädt {len(ids)} Person(en) neu (höchstens ${cap:.2f}). Das dauert einige "
                    "Minuten; die Seite lädt neu, wenn es fertig ist.</div>"
                    + poll_fragment(job.id, "Alles neu laden läuft …")
                )
        if is_htmx(request):
            return HTMLResponse(html)
        return page("Alles neu laden", f"<h1>Alles neu laden</h1>{html}")

    def refresh_page(request: Request, target: str) -> Response:
        from klugscheisser.jobs.refresh import refresh

        try:
            with session_scope() as s:
                out = refresh(s, settings, target)
        except (t.NotFound, ValueError) as exc:
            html = f"<div class='err'>{e(exc)}</div>"
        else:
            parts = [f"News: {e(out.get('news'))}", f"Instagram: {e(out.get('instagram'))}"]
            html = "".join(f"<div class='small'>{x}</div>" for x in parts)
            if out.get("job_id"):
                html += poll_fragment(out["job_id"], "Instagram wird aktualisiert …")
            elif out.get("neu_seit_aufruf"):
                html += f"<div class='small'>{len(out['neu_seit_aufruf'])} neue Einträge. <a href=''>Seite neu laden</a></div>"
        if is_htmx(request):
            return HTMLResponse(html)
        return page("Aktualisieren", f"<h1>Aktualisieren</h1>{html}<p><a href='/'>Zurück</a></p>")

    def refresh_poll(request: Request) -> Response:
        from klugscheisser.jobs.refresh import refresh_status

        job_id = request.path_params["job_id"]
        with session_scope() as s:
            out = refresh_status(s, job_id, wait_s=0)
        if out.get("fehler") and "status" not in out:
            return HTMLResponse(f"<div class='err'>{e(out['fehler'])}</div>")
        status = out["status"]
        if status in ("wartet", "läuft"):
            return HTMLResponse(poll_fragment(job_id, f"Aktualisierung {status} …"))
        if status == "fehlgeschlagen":
            return HTMLResponse(
                f"<div class='err'>Aktualisierung fehlgeschlagen: {e(out.get('fehler'))}</div>"
            )
        n = (out.get("ergebnis") or {}).get("neue_posts", 0)
        html = f"<div class='small'>Fertig: {n} neue Posts. Seite wird neu geladen …</div>"
        return HTMLResponse(html, headers={"HX-Refresh": "true"})

    # --- old question URL → chat ---------------------------------------------------------------------

    def frage_get(request: Request) -> Response:
        qs = "&".join(f"{k}={quote(str(val))}" for k, val in request.query_params.items() if k != "frage")
        return RedirectResponse(f"/chat{'?' + qs if qs else ''}", status_code=303)

    # --- login ---------------------------------------------------------------------------------------

    def login_get(request: Request) -> Response:
        if settings.web_password is None:
            return RedirectResponse("/", status_code=303)
        nxt = request.query_params.get("next", "/")
        return page("Anmelden", login_form(nxt), bare=True)

    async def login_post(request: Request) -> Response:
        form = await request.form()
        nxt = str(form.get("next") or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        ip = access.client_ip(request)
        if wait := access.locked_for(ip):
            minutes = (wait + 59) // 60
            msg = f"Zu viele falsche Versuche. Bitte in {minutes} Minuten wieder versuchen."
            return page("Anmelden", login_form(nxt, msg), 429, bare=True)
        if not access.check_password(settings, str(form.get("password") or "")):
            access.note_failure(ip)
            return page("Anmelden", login_form(nxt, "Falsches Passwort."), 401, bare=True)
        access.note_success(ip)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(
            access.COOKIE,
            access.sign_session(settings),
            max_age=settings.web_session_days * 86400,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return resp

    def logout(request: Request) -> Response:
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(access.COOKIE)
        return resp

    return [
        Route("/", guarded(home), methods=["GET"]),
        Route("/suche", guarded(search), methods=["GET"]),
        Route("/quellen", guarded(sources), methods=["GET"]),
        Route("/brief", guarded(brief), methods=["GET"]),
        Route("/person/{id:int}", guarded(person), methods=["GET"]),
        Route("/person/{id:int}/posts", guarded(person_posts), methods=["GET"]),
        Route("/person/{id:int}/funfacts", guarded(person_funfacts), methods=["POST"]),
        Route("/person/{id:int}/tier", guarded(person_tier), methods=["POST"]),
        Route("/person/{id:int}/foto", guarded(person_photo), methods=["GET"]),
        Route("/person/{id:int}/aussprache", guarded(pronunciation_info), methods=["GET"]),
        Route("/person/{id:int}/aussprache.wav", guarded(pronunciation_audio), methods=["GET"]),
        Route("/team/{id:int}", guarded(team), methods=["GET"]),
        Route("/event/{key}", guarded(event), methods=["GET"]),
        Route("/chat", guarded(chat_new), methods=["GET"]),
        Route("/chat", guarded(chat_create), methods=["POST"]),
        Route("/chats", guarded(chats_all), methods=["GET"]),
        Route("/chat/{id:int}", guarded(chat_view), methods=["GET"]),
        Route("/chat/{id:int}/nachricht", guarded(chat_message), methods=["POST"]),
        Route("/chat/{id:int}/antwort", guarded(chat_answer), methods=["POST"]),
        Route("/frage", guarded(frage_get), methods=["GET"]),
        Route("/frage", guarded(chat_create), methods=["POST"]),
        Route("/hinzufuegen", guarded(add_create), methods=["POST"]),
        Route("/vorbereitung", guarded(prep_create), methods=["POST"]),
        Route("/bewertung/{id:int}", guarded(rate), methods=["POST"]),
        Route("/projekt", guarded(project_get), methods=["GET"]),
        Route("/projekt/vorschlag/{id:int}", guarded(suggestion_post), methods=["POST"]),
        Route("/projekt/{what}", guarded(project_post), methods=["POST"]),
        Route("/bewertungen", guarded(ratings), methods=["GET"]),
        Route("/bewertungen.json", guarded(ratings_json), methods=["GET"]),
        Route("/hinzufuegen/{id:int}", guarded(add_view), methods=["GET"]),
        Route("/hinzufuegen/{id:int}/ja", guarded(add_yes), methods=["POST"]),
        Route("/hinzufuegen/{id:int}/nein", guarded(add_no), methods=["POST"]),
        Route("/hinzufuegen/{id:int}/praezisieren", guarded(add_refine), methods=["POST"]),
        Route("/aktualisieren", guarded(refresh_post), methods=["POST"]),
        Route("/alles-neu", guarded(deep_post), methods=["POST"]),
        Route("/aktualisieren/{job_id:int}", guarded(refresh_poll), methods=["GET"]),
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/logout", logout, methods=["GET", "POST"]),
        Route("/{path:path}", unknown_path, methods=["GET"]),  # must stay last
    ]


def login_form(nxt: str, error: str = "") -> str:
    err = f"<p class='err' role='alert'>{e(error)}</p>" if error else ""
    return f"""<div class="gate"><h1>Anmelden</h1>
<p class="meta">Klugscheisser Karl: Fakten zu {e(wording().players)}, Teams und Turnieren, jede Aussage mit Quelle.</p>{err}
<form method="post" action="/login"><input type="hidden" name="next" value="{e(nxt)}">
<label class="sr" for="pw">Passwort</label>
<input id="pw" type="password" name="password" placeholder="Passwort" autocomplete="current-password" required autofocus>
<button class="btn primary" type="submit">Anmelden</button></form></div>"""


def build_web_app(settings: Settings | None = None) -> Starlette:
    """Standalone app for tests and local development (production mounts web_routes into the MCP app)."""
    return Starlette(routes=web_routes(settings))
