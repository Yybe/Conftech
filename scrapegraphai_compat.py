"""scrapegraphai_compat.py - Make scrapegraphai work with modern langchain.

scrapegraphai 1.76.0 (latest) still does `from langchain_community.chat_models
import ChatOllama`, but langchain-community 0.4+ removed ChatOllama (moved to
langchain_ollama). This shim re-exports it before scrapegraphai is imported.

Call `patch()` once at the start of any module that imports scrapegraphai.
"""

from __future__ import annotations

_patched = False


def patch() -> None:
    global _patched
    if _patched:
        return
    try:
        from langchain_ollama import ChatOllama  # noqa: F401
    except ImportError:
        return  # no langchain_ollama either - let scrapegraphai fail naturally

    import langchain_community.chat_models as chat_models

    if not hasattr(chat_models, "ChatOllama"):
        chat_models.ChatOllama = ChatOllama
        _patched = True


if __name__ == "__main__":
    patch()
    from scrapegraphai.graphs import SmartScraperGraph  # noqa: F401

    print("scrapegraphai imports OK with ChatOllama shim")
