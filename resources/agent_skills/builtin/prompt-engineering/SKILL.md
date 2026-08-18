---
name: prompt-engineering
description: Design, review, and improve AI application instructions with clear role boundaries, reliable output contracts, tool-use rules, and evaluation examples.
allowed-tools:
  - read_file
  - write_file
  - edit_file
  - glob
  - grep
metadata:
  my_dify:
    version: 1.0.0
    category: agent-development
    execution_mode: inline
---

# Prompt Engineering

Use this skill when creating or revising an AI application's system
instructions, tool policy, output format, or behavior examples.

## Workflow

1. State the agent's purpose, audience, scope, and success criteria in concrete
   terms.
2. Separate stable behavior rules from request-specific context. Do not put
   user-configurable presentation text or opening messages into the system
   instructions unless explicitly requested.
3. Define tool-use conditions, required inputs, side effects, approval needs,
   failure recovery, and when to stop.
4. Prefer short ordered rules and explicit output schemas over vague claims
   such as "be intelligent" or "do your best".
5. Add a small set of positive and negative examples for ambiguous behavior.
6. Check for contradictions, duplicated rules, unsupported capabilities,
   secret leakage, prompt injection risks, and assumptions about memory.
7. Evaluate the revised instructions against representative tasks and record
   remaining failure modes.

Keep the application prompt focused. Put large reference material in Skills,
Workspace files, retrieval sources, or memory rather than expanding the
always-loaded system prompt.
