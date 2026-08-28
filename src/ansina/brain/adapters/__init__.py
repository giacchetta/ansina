"""`BrainProvider` adapters. See issue #12.

`openai_compat.py` is the only adapter this milestone ships — an OpenAI-compatible chat
completions client that works against any endpoint speaking that protocol (OpenAI
itself, an Anthropic-via-compat-shim, OpenRouter, a self-hosted server), selected via
`[brain] base_url`/`api_key`/`model`. A local 35B adapter and multi-provider routing are
explicitly out of scope for issue #12.
"""
