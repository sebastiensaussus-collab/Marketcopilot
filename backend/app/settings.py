from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    fred_api_key: str = ""

    db_path: str = "market_copilot.db"

    # Full SP100 (100) as of tonight -- was capped at 30 pending the pipeline proving out
    # (see git history: yfinance call timeouts, quote caching, NaN-safety all landed this
    # session). Scanning more costs only wall-clock time (yfinance, free), not Claude spend
    # -- equity_shortlist_size below is what actually controls synthesis cost, unchanged.
    equity_universe_size: int = 100
    equity_shortlist_size: int = 10
    synthesis_concurrency: int = 5  # concurrent Claude calls during a refresh

    # IBKR connects to a locally running TWS/IB Gateway process that you log into yourself
    # via its own GUI -- this app never sees your IBKR credentials, only this local socket.
    # Default port 7497 = TWS paper trading. Live TWS = 7496, Gateway live = 4001, Gateway
    # paper = 4002 -- set ibkr_port to match whichever you have running.
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 17

    # Email reports. smtp_app_password is a Gmail "App Password" (myaccount.google.com ->
    # Security -> 2-Step Verification -> App passwords) -- never your real Google password.
    reports_enabled: bool = True
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_app_password: str = ""
    report_recipient: str = ""  # defaults to smtp_user if left blank
    morning_report_time: str = "07:30"
    lunch_report_time: str = "12:30"
    evening_report_time: str = "21:00"

    # Urgent alerts: a separate, tighter-cadence check (satellite price moves,
    # concentration breaches) distinct from the 3x/day digest reports above -- see
    # app/alerts.py. 20min balances catching things same-day against hammering free APIs
    # (yfinance rate-limiting under sustained load was live-verified this session).
    # alert_cooldown_hours prevents the same still-unresolved condition from re-firing a
    # new email every single check.
    alerts_enabled: bool = True
    alert_check_interval_minutes: int = 20
    alert_cooldown_hours: int = 12


settings = Settings()
