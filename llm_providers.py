"""
================================================================================
  Phantom Inventory Hunter — LLM Provider Factory
================================================================================
  Author:      Mohith Kunta
  GitHub:      https://github.com/m-kunta
  Description: Provider-agnostic AI layer supporting Gemini, OpenAI, Anthropic,
               Groq, and Ollama. Add a new provider by implementing a private
               _call_<name>() function and registering it in PROVIDER_DEFAULTS
               and get_llm_response().
================================================================================
"""

import os

# ─────────────────────────────────────────────────────────────────────────────
# PROVIDER REGISTRY
# Maps display name → default model + corresponding .env key name
# Author: Mohith Kunta | https://github.com/m-kunta
# ─────────────────────────────────────────────────────────────────────────────

PROVIDER_DEFAULTS: dict[str, dict] = {
    "Gemini":    {"model": "gemini-2.5-flash",          "key_env": "GEMINI_API_KEY"},
    "OpenAI":    {"model": "gpt-4o-mini",               "key_env": "OPENAI_API_KEY"},
    "Anthropic": {"model": "claude-3-5-haiku-latest",   "key_env": "ANTHROPIC_API_KEY"},
    "Groq":      {"model": "llama-3.3-70b-versatile",   "key_env": "GROQ_API_KEY"},
    "Ollama":    {"model": "llama3.2",                  "key_env": None},  # Local, no key needed
}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — call this from app.py
# ─────────────────────────────────────────────────────────────────────────────

def get_llm_response(prompt: str, provider: str, model: str) -> str:
    """
    Route a text prompt to the selected LLM provider and return the response.

    Args:
        prompt:   The text prompt to send.
        provider: Provider display name (must match a key in PROVIDER_DEFAULTS).
        model:    Model identifier string (e.g., 'gemini-2.5-flash').

    Returns:
        The LLM response as a plain string, or a user-friendly error message.
    """
    dispatch = {
        "Gemini":    _call_gemini,
        "OpenAI":    _call_openai,
        "Anthropic": _call_anthropic,
        "Groq":      _call_groq,
        "Ollama":    _call_ollama,
    }

    handler = dispatch.get(provider)
    if not handler:
        return f"❌ Unknown provider: '{provider}'. Choose from: {list(dispatch.keys())}"

    return handler(prompt, model)


# ─────────────────────────────────────────────────────────────────────────────
# PRIVATE PROVIDER IMPLEMENTATIONS
# Each function is isolated — failures in one never affect the others.
# ─────────────────────────────────────────────────────────────────────────────

def _call_gemini(prompt: str, model: str) -> str:
    """Google Gemini via the official google-genai SDK."""
    try:
        from google import genai
        api_key = os.getenv("GEMINI_API_KEY", "")
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        return response.text
    except ImportError:
        return "❌ `google-genai` not installed. Run: pip install google-genai"
    except Exception as e:
        return f"🚨 Gemini error: {e}"


def _call_openai(prompt: str, model: str) -> str:
    """OpenAI (GPT-4o, GPT-4o-mini, etc.) via the openai SDK."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350,
        )
        return response.choices[0].message.content
    except ImportError:
        return "❌ `openai` not installed. Run: pip install openai"
    except Exception as e:
        return f"🚨 OpenAI error: {e}"


def _call_anthropic(prompt: str, model: str) -> str:
    """Anthropic Claude via the anthropic SDK."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        message = client.messages.create(
            model=model,
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except ImportError:
        return "❌ `anthropic` not installed. Run: pip install anthropic"
    except Exception as e:
        return f"🚨 Anthropic error: {e}"


def _call_groq(prompt: str, model: str) -> str:
    """Groq (Llama, Mixtral) via the groq SDK — extremely fast inference."""
    try:
        from groq import Groq
        client = Groq(api_key=os.getenv("GROQ_API_KEY", ""))
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350,
        )
        return response.choices[0].message.content
    except ImportError:
        return "❌ `groq` not installed. Run: pip install groq"
    except Exception as e:
        return f"🚨 Groq error: {e}"


def _call_ollama(prompt: str, model: str) -> str:
    """
    Ollama — 100% local inference, no API key required.
    Requires Ollama to be running: https://ollama.com
    Pull the model first: ollama pull llama3.2
    """
    try:
        import ollama
        response = ollama.generate(model=model, prompt=prompt)
        return response["response"]
    except ImportError:
        return "❌ `ollama` not installed. Run: pip install ollama"
    except Exception as e:
        return (
            f"🚨 Ollama error: {e}\n\n"
            "Make sure Ollama is running locally (https://ollama.com) "
            f"and the model is pulled: `ollama pull {model}`"
        )
