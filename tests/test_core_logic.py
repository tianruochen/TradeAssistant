"""关键"算钱"逻辑的单元测试(隔离,无网络/无真实数据)。
运行:  python3 -m pytest tests/ -q
"""
from core.tools import ledger_tools
from core import constraints, performance, attribution


# ── FIFO 已实现盈亏 / 胜率 ──
def test_realized_pnl_fifo_and_winrate(monkeypatch):
    monkeypatch.setattr(ledger_tools, "trades", lambda: [
        {"action": "buy", "symbol": "AA", "shares": 100, "price": 10},
        {"action": "sell", "symbol": "AA", "shares": 100, "price": 12},   # +200 赢
        {"action": "buy", "symbol": "BB", "shares": 200, "price": 5},
        {"action": "sell", "symbol": "BB", "shares": 100, "price": 4},     # -100 输
    ])
    r = ledger_tools.realized_pnl()
    assert r["total"] == 100.0
    assert r["win_rate"] == 50.0
    assert r["closed_trades"] == 2


def test_realized_pnl_partial_lots(monkeypatch):
    monkeypatch.setattr(ledger_tools, "trades", lambda: [
        {"action": "buy", "symbol": "AA", "shares": 100, "price": 10},
        {"action": "buy", "symbol": "AA", "shares": 100, "price": 20},
        {"action": "sell", "symbol": "AA", "shares": 150, "price": 15},    # 100@10 +500, 50@20 -250 => +250
    ])
    assert ledger_tools.realized_pnl()["total"] == 250.0


# ── 硬约束校验 ──
def test_constraints_flags(monkeypatch):
    monkeypatch.setattr(constraints, "parse_holdings", lambda: {
        "positions": [
            {"name": "甲", "code": "1", "weight_pct": 30.0, "pnl_pct": 5, "sector": "医药", "is_etf": False},
            {"name": "乙", "code": "2", "weight_pct": 10.0, "pnl_pct": 0, "sector": "医药", "is_etf": False},
            {"name": "丙", "code": "3", "weight_pct": 10.0, "pnl_pct": 0, "sector": "医药", "is_etf": False},
        ],
        "cash_pct": 5.0, "total_assets": 100.0,
    })
    r = constraints.check("shake")
    rules = {v["rule"] for v in r["violations"]}
    assert not r["ok"]
    assert any("现金" in x for x in rules)        # 现金5%<10%
    assert any("单票" in x for x in rules)         # 甲30%>20%
    assert any("同板块" in x for x in rules)       # 医药3只>2


def test_constraints_ok(monkeypatch):
    monkeypatch.setattr(constraints, "parse_holdings", lambda: {
        "positions": [{"name": "甲", "code": "1", "weight_pct": 15.0, "pnl_pct": 1, "sector": "医药", "is_etf": False}],
        "cash_pct": 40.0, "total_assets": 100.0,
    })
    assert constraints.check("shake")["ok"]


# ── 最大回撤 ──
def test_max_drawdown():
    assert performance._max_drawdown([100, 120, 90, 110]) == 25.0   # 120→90
    assert performance._max_drawdown([100, 101, 102]) == 0.0


# ── 决策方向判定 ──
def test_attribution_side():
    assert attribution._side("买入") == "long"
    assert attribution._side("加仓") == "long"
    assert attribution._side("清仓") == "short"
    assert attribution._side("减仓") == "short"
    assert attribution._side("观察") is None
