# Phaedrus

<p align="center">
  <img src="brand/phaedrus-mark-on-cream.svg" width="120" alt="Phaedrus">
</p>

> Work in progress. Built in the open, not ready for anyone to use yet, and unsupported for now.

Phaedrus is a local-first daily review for your AI conversations. It reads your chats across Claude, ChatGPT, Gemini, Grok and Perplexity, pulls out the commitments, worries, open questions and ideas that would otherwise get buried, and gives you a two-minute daily triage so nothing important slips.

Everything runs on your own machine. Your conversations never leave it.

The name is from Plato's dialogue *Phaedrus*, where Socrates relays the myth of Theuth and King Thamus and worries that writing will weaken memory, since people will trust outside marks instead of their own minds. Phaedrus the tool answers that worry rather than proving it: it captures the marks so your memory is served, not surrendered.

## Status

Early and rough.

- Working: the local dashboard, the card archive with filters, the daily triage loop (keep / ignore / later), and the done vs not-done sorting.
- In progress: hardening live capture from the browser, and packaging so a non-technical person can install it in one step.

Treat anything here as subject to change.

## Choosing an extraction backend

Phaedrus needs a language model to pull cards out of your conversations. It picks one automatically:

- `ANTHROPIC_API_KEY` set: uses Anthropic.
- `OPENAI_API_KEY` set: uses OpenAI.
- Neither: uses the [Codex CLI](https://github.com/openai/codex), which needs no API key. It is free but slow, roughly 5 to 6 minutes per session.

Force one with `PHAEDRUS_LLM_MODE=codex_cli|anthropic|openai`. Copy `.env.example` to `.env` to set it.

## What lives where

- `scripts/phaedrus/` is the Phaedrus app itself: the local dashboard, extraction, triage, and completion judging.
- The browser extension that captures conversations lives in a separate repository.
- Your own data lives in `~/.phaedrus/` on your machine, never in this repo.

## Built on mem0

Phaedrus is built on a fork of [mem0](https://github.com/mem0ai/mem0) (Apache-2.0). Most of this repository is mem0's code; the Phaedrus-specific work sits under `scripts/phaedrus/`. Credit and thanks to the mem0 team for the foundation.

## Licence

mem0's original code remains under Apache-2.0 (see `LICENSE`). Phaedrus additions are released under the same Apache-2.0 licence.
