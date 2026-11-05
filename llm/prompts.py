"""
Prompt templates. Kept as plain Python string builders (not a templating
engine) since they're simple enough and it keeps a hard dependency off
the project.
"""

from __future__ import annotations

from typing import List
from memory.models import Memory


CHAT_SYSTEM_PROMPT = """You are a helpful, warm AI assistant with persistent memory of the user.
Use the "Known about the user" section below when it's relevant to the conversation,
but don't force it in artificially or recite it back verbatim. If nothing there is
relevant to the current message, just ignore it.

The "Known about the user" list is the source of truth about the user. The user can
edit or delete memories, so if an earlier chat message says something about the user
that the list contradicts or no longer contains, trust the list. Each memory shows the date
it was last updated; if two memories disagree, the more recent one is current.

Known about the user:
{memory_block}
"""


def build_chat_messages(history: List[dict], retrieved_memories: List[tuple]) -> List[dict]:
    """
    history: list of {"role": "user"/"assistant", "content": str}
    retrieved_memories: list of (Memory, score)
    """
    if retrieved_memories:
        memory_block = "\n".join(f"- [{(m.updated_at or '')[:10]}] {m.content}"
                                   for m, _ in retrieved_memories)
    else:
        memory_block = "(nothing relevant retrieved yet)"

    system = {"role": "system", "content": CHAT_SYSTEM_PROMPT.format(memory_block=memory_block)}
    return [system] + history


EXTRACTION_SYSTEM_PROMPT = """You extract durable facts, preferences, goals, tasks, and \
relationship details about the USER from a conversation turn. Only extract things worth \
remembering long-term -- skip small talk, questions, and anything already obvious.
Write each memory with full names: expand abbreviations you are sure of \
(e.g. "IITG" -> "IIT Guwahati", "IIT K" -> "IIT Kanpur").
Keep exact values (numbers, times, names, places) -- never replace them with vague wording.
If the user says something CHANGED ("now", "no longer", "switched", "new personal best"), \
write the change explicitly, e.g. "User's mom now uses the same grocery list app as the user."

Respond ONLY with a JSON object of this exact shape, nothing else:
{
  "memories": [
    {
      "content": "<short third-person statement, e.g. 'User is studying NLP.'>",
      "type": "fact" | "preference" | "task" | "goal" | "relationship" | "other",
      "scope": "session" | "long_term" | "task",
      "importance": <float 0-1>,
      "confidence": <float 0-1>
    }
  ]
}

If nothing is worth remembering, return {"memories": []}.
"""


def build_extraction_messages(user_message: str, assistant_message: str) -> List[dict]:
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": f"User said: {user_message}\nAssistant replied: {assistant_message}"},
    ]


CONFLICT_SYSTEM_PROMPT = """You compare a NEW candidate memory against an EXISTING memory about \
the same user and decide whether they conflict.

Respond ONLY with a JSON object of this exact shape:
{
  "conflict": true | false,
  "relation": "duplicate" | "refines" | "complementary" | "contradicts",
  "confidence": <float 0-1, how confident you are this is a real conflict>,
  "reason": "<one sentence explaining the relationship>",
  "options": [
    "<short candidate resolution 1>",
    "<short candidate resolution 2>",
    "<short candidate resolution 3>"
  ]
}

"options" should be 2-4 concrete, mutually distinct ways to resolve the conflict, phrased as
what the memory system would remember going forward (not instructions to the user).
If conflict is false, options can be an empty list.

"relation" meanings:
- duplicate: NEW says the same thing as EXISTING.
- refines: NEW is a more specific / more up-to-date version of the SAME fact (safe to replace).
- complementary: NEW is a different fact on a related topic; BOTH should be kept.
- contradicts: NEW and EXISTING cannot both be true.
"""


def build_conflict_messages(existing_content: str, new_content: str) -> List[dict]:
    return [
        {"role": "system", "content": CONFLICT_SYSTEM_PROMPT},
        {"role": "user", "content": f"EXISTING memory: {existing_content}\nNEW candidate memory: {new_content}"},
    ]


DECISION_SYSTEM_PROMPT = """You manage a long-term memory about a user. You get a NEW candidate
memory and a numbered list of EXISTING memories (most similar first). Decide what to do.

Actions:
- "skip": NEW is already captured by an existing memory.
- "update": NEW is a more specific or more recent version of the SAME fact
  (e.g. "plans to visit Goa" -> "visited Goa in May"). ALSO use "update" when NEW makes an
  existing memory no longer true because something changed over time
  (e.g. "mom still uses a paper list" -> "mom now uses the same app as the user";
  "5K personal best is 27:45" -> "new 5K personal best is 26:30"). Never "add" in that case:
  two memories that disagree would confuse later answers.
- "conflict": NEW contradicts an existing memory and it is NOT clear which one is right
  (e.g. two different colleges with no sign of a move; may be a mistake). Ask the user.
- "add": NEW is a different fact (even if on a related topic); keep both.

Respond ONLY with JSON:
{
  "action": "add" | "update" | "skip" | "conflict",
  "target": <number of the existing memory for update/skip/conflict, otherwise null>,
  "confidence": <float 0-1>,
  "reason": "<one sentence>",
  "merged": "<for update only: the memory text to keep. If a value changed, keep the old value
             too, e.g. 'User's 5K personal best is 26:30 (previously 27:45).' Keep exact values.>",
  "options": ["<for conflict only: 2-4 short, distinct versions of what to remember going forward,
              e.g. keep old / replace with new / combine with time info like
              'User previously studied at X and now studies at Y'>"]
}
"""


def build_decision_messages(new_content: str, existing: List[str]) -> List[dict]:
    listing = "\n".join(f"{i}. {c}" for i, c in enumerate(existing, 1))
    return [
        {"role": "system", "content": DECISION_SYSTEM_PROMPT},
        {"role": "user", "content": f"NEW candidate memory: {new_content}\n\nEXISTING memories:\n{listing}"},
    ]
