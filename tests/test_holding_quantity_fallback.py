from types import SimpleNamespace

import pytest

from src.api.market_data import PortfolioReporter


class _FakeClient:
    async def get_holdings(self):
        return [
            SimpleNamespace(
                tradingsymbol="TCS",
                exchange="NSE",
                quantity="0",
                realised_quantity="0",
                opening_quantity="125",
                t1_quantity="0",
                used_quantity="0",
                average_price=250.0,
                last_price=270.0,
                close_price=262.0,
                pnl=0.0,
                day_change=0.0,
            )
        ]


@pytest.mark.asyncio
async def test_holding_quantity_prefers_non_zero_string_fallbacks() -> None:
    reporter = PortfolioReporter(_FakeClient())

    data = await reporter.get_holdings_report()

    assert data["holdings"][0]["quantity"] == 125


from src.api.market_data import _resolve_holding_quantity
from src.data.models import Holding


def _holding(**kw):
    base = {"tradingsymbol": "X", "average_price": 100.0, "last_price": 110.0}
    return Holding.model_validate({**base, **kw})


def test_mtf_only_holding_uses_mtf_quantity():
    h = _holding(quantity=0, t1_quantity=0, mtf={"quantity": 50, "used_quantity": 0, "average_price": 1554.6})
    assert h.mtf["quantity"] == 50          # nested object survives model parsing
    assert _resolve_holding_quantity(h) == 50


def test_settled_t1_and_mtf_legs_are_summed():
    h = _holding(quantity=40, t1_quantity=10, mtf={"quantity": 5})
    assert _resolve_holding_quantity(h) == 55


@pytest.mark.parametrize("sym,avg,ltp,pnl,qty", [
    ("ATHERENERG", 1554.60, 1470.00, -4230.00, 50),
    ("EMCURE", 1936.97, 1820.60, -3491.20, 30),
    ("LAURUSLABS", 2021.80, 2059.00, 1860.00, 50),
])
def test_quantity_implied_by_broker_pnl_when_all_fields_are_zero(sym, avg, ltp, pnl, qty):
    h = _holding(tradingsymbol=sym, quantity=0, average_price=avg, last_price=ltp, pnl=pnl)
    assert _resolve_holding_quantity(h) == qty


def test_non_integer_implied_quantity_is_not_trusted():
    h = _holding(quantity=0, average_price=100.0, last_price=103.0, pnl=10.0)
    assert _resolve_holding_quantity(h) == 0
