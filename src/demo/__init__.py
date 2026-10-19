"""Demo mode: a simulated market and broker that stand in for Kite Connect.

Nothing in this package talks to Zerodha. Demo sessions run the real terminal
code (strategies, scanner, backtests, execution engine, agents) against
``DemoKiteClient`` / ``DemoTicker``; every order fills in a private sandbox.
"""
