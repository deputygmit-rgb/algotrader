"""The broker's login callback may arrive twice (a browser loading the page twice, a double
click): the single-use token is exchanged once and the repeat gets the same answer."""
import asyncio
from types import SimpleNamespace


def _service():
    from src.api.service import TradingService

    state = {"auth": False}

    class Svc(TradingService):
        is_authenticated = property(lambda self: state["auth"])

    svc = Svc.__new__(Svc)
    calls = []

    async def generate_session(token):
        calls.append(token)
        await asyncio.sleep(0.01)
        if calls.count(token) > 1:
            raise RuntimeError("Token is invalid or has expired.")
        state["auth"] = True
        return SimpleNamespace(user_id="AB1234", user_name="Test")
    svc._auth = SimpleNamespace(generate_session=generate_session)

    async def resume():
        return {}
    svc.resume_after_restart = resume
    svc._task_exception_handler = lambda t: None
    return svc, calls


def test_a_repeated_callback_is_answered_without_a_second_exchange():
    svc, calls = _service()

    async def go():
        a = await svc.authenticate("tok-1")
        b = await svc.authenticate("tok-1")
        return a, b
    a, b = asyncio.run(go())
    assert a == b == {"user_id": "AB1234", "user_name": "Test"} and calls == ["tok-1"]


def test_simultaneous_callbacks_wait_for_the_first():
    svc, calls = _service()

    async def go():
        return await asyncio.gather(svc.authenticate("tok-2"), svc.authenticate("tok-2"))
    a, b = asyncio.run(go())
    assert a == b and calls == ["tok-2"]


def test_a_new_token_is_exchanged_again():
    svc, calls = _service()

    async def go():
        await svc.authenticate("tok-3")
        await svc.authenticate("tok-4")
    asyncio.run(go())
    assert calls == ["tok-3", "tok-4"]


def test_the_service_worker_answers_every_page_load_itself():
    import os

    src = open(os.path.join(os.path.dirname(__file__), "..", "static", "sw.js"), encoding="utf-8").read()
    navigate, never = src.index("req.mode === 'navigate'"), src.index("NEVER.some(")
    assert navigate < never        # page loads (the /auth/ callback included) never fall through to a second fetch
