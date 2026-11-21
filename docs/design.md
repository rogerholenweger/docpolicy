---
subject: inbox-triage-design
status: active
---

# Inbox triage — design note

Scope: the `inbox` class mode and `docpolicy triage`. Why captures get promoted
the way they do, and which properties are guaranteed.

## Flow

1. **Scan** — every file under each `inbox` class path is a capture. Dotfiles are
   skipped, so a `.gitkeep` drop zone is not itself a capture. Capture text is
   read for `.md`, `.markdown`, `.txt`; PDF/HTML/DOCX/EPUB/RTF/ODT are routed to
   the `convert` path instead of being parsed inline.
2. **Profile** — each existing doc contributes a profile: subject, tags, title,
   description, H1, and a token set built from those fields.
3. **Score** — a capture is compared to every profile by Jaccard overlap
   (`|intersection| / |union|`) over token sets. The top three scores become the
   `related` list on the proposal; the top one seeds subject and tags, and its
   tags are carried over so related docs stay discoverable through `C10`.
4. **Flag** — two flags are mechanical, not advisory: a capture whose subject is
   already `status: active` is a *supersession candidate* (the invariant forbids
   a second active doc), and a top-two score margin under 25% (with top score
   above 0.04) is *ambiguous topic*, because a coin-flip placement is worse than
   a human decision.
5. **Propose** — destination, subject, title, description, tags, related docs and
   flags, emitted as text or `--json`.
6. **Promote** — only with `--apply`. Writes the doc with frontmatter, links it to
   its related docs, and optionally queues a task (`--queue`, when
   `sources.issues` is configured).

## Guarantees

- **Deterministic first.** Triage works with no model configured and no network.
  The whole proposal is computed from capture text plus corpus.
- **The model only labels.** An optional LLM refines title, description, tags and
  the subject suggestion. It never chooses a destination and is never required;
  any error degrades to the deterministic proposal, and `prop.llm` records why a
  model was or was not used.
- **Nothing is destructive without `--apply`.** Triage proposes; the caller
  applies. Cost of a bad proposal is one review, not one moved file.
- **Subject is identity, not topic.** Promotion never reuses an active subject;
  related topics are connected through tags. That keeps the one-active-doc
  invariant true by construction rather than by discipline.

## Aging

Captures are exempt from `FM`/`C2`/`C9` while they sit in the inbox — an
unpromoted capture is not yet a doc. `C11` ages them: a capture older than the
class's `max_age_days` becomes a finding, so an inbox cannot silently rot.

## Agent integration

The Hermes plugin surfaces pending captures through `pre_llm_call`, which is a
*Directive* hook — its return value is injected into the turn, unlike observer
hooks (`on_session_start`) whose returns are ignored. The notice is one line,
TTL-cached (120s) because the hook fires every turn, and gated by a cheap text
scan so repos without an inbox class never pay for a subprocess. The
`docpolicy_triage` tool carries an 8s budget; a model-enabled triage needs much
longer (roughly a minute per doc, 45s HTTP timeout), so run the CLI directly for
LLM labels.

## Known limits

- Related-doc scoring is bag-of-words overlap. No embeddings, no semantic
  similarity — two docs about the same thing in different words score low.
- Tag carry-over is a prior, not a judgement: a wrong top match propagates its
  tags onto the capture. The `ambiguous topic` flag is the mitigation.
- No conversion is performed during scan; non-text captures must be converted
  before triage can read them.
- `--queue` writes to the real task store named by `sources.issues`. Test
  artifacts written there must be deleted by id.
