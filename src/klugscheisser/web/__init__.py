"""Own web app (v3 P6): search, person/team/event pages, question box. Server-rendered HTML + htmx."""

from klugscheisser.web.engine import FakeAnswerEngine, get_answer_engine
from klugscheisser.web.routes import build_web_app, web_routes

__all__ = ["FakeAnswerEngine", "build_web_app", "get_answer_engine", "web_routes"]
