"""
webadmin – Modular web administration dashboard for the Telegram bot.

Mount onto the existing FastAPI app (single exposed port on Render):

    from webadmin import mount_admin
    mount_admin(app)
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def mount_admin(app: Any) -> None:
    """Attach the admin API + SPA to the shared FastAPI application."""
    from webadmin import keywords_store, settings_store
    from webadmin.routes import router

    app.include_router(router)

    # The hot-reload engine resolves the bot lazily from app.state.bot_ref
    keywords_store.set_app_reference(app)

    # Re-apply persisted runtime settings
    try:
        settings_store.apply_persisted_on_startup()
    except Exception as e:
        logger.warning(f"runtime settings restore skipped: {e}")

    logger.info("webadmin mounted: /admin (SPA) + admin REST APIs")
