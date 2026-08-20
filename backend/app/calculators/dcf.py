"""Simple single-stage-growth-then-terminal DCF, enough to flag a rough margin of safety."""

from app.numeric import finite


def fair_value_per_share(
    free_cashflow: float,
    shares_outstanding: float,
    growth_rate: float,
    discount_rate: float,
    terminal_growth_rate: float,
    projection_years: int = 5,
) -> float | None:
    # discount_rate == -1 would divide by zero below ((1 + discount_rate) ** year == 0);
    # not reachable from the one real caller (app/screener.py uses a fixed 0.09), but this
    # is a general-purpose function -- cheap to guard rather than assume every future
    # caller respects that.
    if not free_cashflow or not shares_outstanding or discount_rate <= terminal_growth_rate or discount_rate == -1:
        return None

    pv_sum = 0.0
    fcf = free_cashflow
    for year in range(1, projection_years + 1):
        fcf *= 1 + growth_rate
        pv_sum += fcf / (1 + discount_rate) ** year

    terminal_value = (fcf * (1 + terminal_growth_rate)) / (discount_rate - terminal_growth_rate)
    pv_terminal = terminal_value / (1 + discount_rate) ** projection_years

    enterprise_value = pv_sum + pv_terminal
    return finite(enterprise_value / shares_outstanding)


def margin_of_safety(fair_value: float | None, current_price: float | None) -> float | None:
    if not fair_value or not current_price:
        return None
    return finite(fair_value / current_price - 1)
