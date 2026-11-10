"""Groq LLM client (via LangChain) with call counting per request."""
from __future__ import annotations

from functools import lru_cache

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from .config import get_settings


class LLMNotConfigured(RuntimeError):
    pass


@lru_cache
def _client():
    s = get_settings()
    if not s.groq_api_key:
        raise LLMNotConfigured("GROQ_API_KEY is not set. Add it to your .env file.")
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=s.groq_model,
        temperature=s.llm_temperature,
        api_key=s.groq_api_key,
        max_retries=2,
        timeout=40,
    )


def call_llm(system: str, human: str) -> str:
    """Single LLM call. Templates are passed through ChatPromptTemplate with
    variables so curly braces inside schemas/SQL can never break formatting."""
    prompt = ChatPromptTemplate.from_messages([("system", "{system}"), ("human", "{human}")])
    chain = prompt | _client() | StrOutputParser()
    return chain.invoke({"system": system, "human": human}).strip()


def llm_configured() -> bool:
    return bool(get_settings().groq_api_key)
