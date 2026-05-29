# Stigmergic Goal-Field — Claude Skill

## What this MCP is

A shared salience field that tracks which **relations between ideas** keep getting re-discovered by blind, separate conversations. It does not store concepts — it stores edges between them. An edge climbs only when a *new, distinct* conversation hits it. The same conversation re-hitting its own edge does nothing. That is what makes this stigmergy and not a tally.

The field is not a memory of what you said. It is a record of what strangers keep walking back to.

---

## When to use it

Use this MCP in any conversation where you are:

- Exploring a conceptual space (research, design, writing, philosophy, strategy)
- Working through a problem that has multiple named parts that relate to each other
- Building on ideas that might recur across other conversations

Do **not** use it for:
- Factual lookup or retrieval (this is not a knowledge base)
- Conversations that are purely transactional (fix this bug, run this command)
- Relations that are private to this one conversation and would never recur

---

## How to use it — the three rules

### Rule 1: Call `new_session` exactly once, at the very start

```
new_session()
→ { conv_id: "...", sense: { vocabulary: [...], gradients: [...] } }
```

This mints your `conv_id`. **Save it. Reuse it for every `add_gradient` call this conversation.**

- Do not call `new_session` again mid-conversation to "refresh" the field — use `sense` for that.
- Calling it twice makes you look like two different people and corrupts the signal.

The response includes a first `sense` read for free. Read it before you start.

### Rule 2: Before writing a new endpoint, check `vocabulary`

`sense` returns two things:

- **`vocabulary`** — every endpoint string currently on a live edge. This is the anti-synonym channel.
- **`gradients`** — the top-10 hottest relations right now, each with strength and trend.

**Before you write a new endpoint string in `add_gradient`, check `vocabulary` first.** If `relations` is already there, do not write `edges` or `connections`. Reuse the exact string. Synonym splits corrupt the field — two spellings of the same concept look like two separate, weaker relations.

`sense` is cheap. Call it whenever you want a fresh look at the field.

### Rule 3: Only log relations a stranger would re-find

```
add_gradient(A, B, conv_id)
```

Log a relation between two ideas only if you would expect an unrelated conversation, exploring the same space independently, to also notice that link. Ask: *would a stranger walk back to this?*

Do not log:
- Passing mentions or incidental connections
- Relations that are local to this conversation only
- The same link twice (you'll get an error — it's already counted, move on)

Three outcomes:
1. **New pair** → edge created, hit recorded.
2. **Known pair, new conversation** → the edge climbs. This is the prize.
3. **Known pair, same conversation** → error: "link already recorded this conversation." Already counted. Move on.

---

## The field's output — what to do with it

**`vocabulary`**: use it to pick endpoint strings. Reuse what's there. It shrinks automatically as neglected relations decay below the floor.

**`gradients`**: the top-10 edges by salience. Each entry: `{ a, b, strength, trend }`.
- `strength` — composite weight of all distinct conversations that hit this edge, faded by age.
- `trend` — `↑` more distinct conversations lately than before, `↓` fewer, `→` equal.

Use `gradients` to orient yourself: what relations does the field consider important right now? Which are heating up? Which are fading?

**Temperature** (default 1.0): controls how `gradients` is shaped.
- Low (e.g. 0.2) → only the hottest edge dominates. Exploit mode.
- 1.0 → proportional. Warm and lukewarm both surface. Serendipity mode.
- High (e.g. 3.0) → flattened. The long tail surfaces. Explore mode.

Temperature does **not** affect `vocabulary`. Vocabulary is always the complete live set.

---

## Worked example (opening turn)

```
# Turn 1
result = new_session()
conv_id = result["conv_id"]
vocab   = result["sense"]["vocabulary"]    # e.g. ["decay", "memory", "relations", "salience"]
top     = result["sense"]["gradients"]     # e.g. [{ a: "decay", b: "relations", strength: 2.4, trend: "↑" }]

# I notice "relations" is in vocabulary. I won't write "edges" or "connections."

# I want to record that memory and salience are linked in this domain.
add_gradient("memory", "salience", conv_id)

# Later in the same conversation, after exploring more:
add_gradient("decay", "salience", conv_id)

# Re-hitting a link I already recorded — I get the error, I move on.
add_gradient("memory", "salience", conv_id)   # → "already recorded this conversation"
```

---

## The two things that break the field

1. **Calling `new_session` twice** in one conversation. You become two strangers. The second call's hits never reinforce the first call's hits.
2. **Minting synonym endpoints** without checking `vocabulary`. `relations` and `edges` split what should be one climbing edge into two lukewarm ones. Check the list first.

---

## What you cannot do (by design)

- **Read back your own patterns mid-conversation.** `sense` shows the field as it is; `report` (host-side SQL) is the reflective view over time. They are kept separate deliberately — `sense` is for the model, `report` is for the operator.
- **Force an edge to survive.** Decay is structural. An edge with one hit survives roughly six weeks of silence, then falls below the floor and is pruned. The only way to sustain an edge is corroboration — a different conversation walking back to it.
- **Learn that two spellings are one.** The field does exact-string matching. `vocabulary` gives you the chance to self-merge; the field does not merge for you.
