# DeepAgents Multi-Agent Platform Final Implementation

## Goal

Implement the complete target architecture defined by
`docs/deepagents-v2-plan`. The result must be a production-capable multi-agent
platform, not a staged, simplified, or placeholder implementation.

## In Scope

- DeepAgents-native graph assembly and middleware ordering.
- Persistent LangGraph checkpointer lifecycle with stable conversation threads.
- DB-backed workspace exposed through the native FilesystemMiddleware,
  including summary history offload and tenant/conversation isolation.
- Platform-builtin Skills loaded from a public versioned read-only directory,
  listed in the Creator web UI, mounted to the root agent through AppConfig,
  and optionally narrowed to a subset for each synchronous subagent.
- Layered L1-L4 context assembly and structured memory fact recall/extraction.
- Complete synchronous sub-agent configuration with `mode=sync` and
  `context_passing=NONE`; delegation uses the native DeepAgents `task` tool.
- Creator APIs for the builtin Skill catalog and AppConfig bindings, plus
  Creator/end-user APIs for workspace and memory.
- Creator UI for synchronous agent teams, builtin Skill browsing/mounting,
  workspace, and read-only memory; end-user
  UI for owned memory and conversation-history files.
- Existing SSE contract, review behavior, tool integration, and all current
  invocation entry points.
- Backend and frontend regression tests, production build, and scoped commits.

## Out of Scope

- Replacing the existing visual workflow execution graph.
- Asynchronous subagents, parent-history `SUMMARY/RECENT_K` policies, and HITL.
- Tenant/Creator Skill content upload, editing, version CRUD, or arbitrary
  scripts; V1 only mounts platform-builtin packages.
- Introducing a custom replacement for DeepAgents/LangChain native
  Filesystem, Skills, Summarization, SubAgent, AsyncSubAgent, or saver
  components.
- Staging unrelated documentation, IDE state, task artifacts, or user changes.

## Constraints

- The documents under `docs/deepagents-v2-plan` are the decision source.
- Framework-native-first is a project-level hard constraint: use DeepAgents
  first, then LangChain/LangGraph primitives, then protocol adapters. A custom
  component is allowed only with documented missing capability, rejected
  native alternatives, and automated acceptance coverage.
- Custom code is limited to business persistence, tenant isolation, context
  assembly, event translation, and adapters required to expose my-dify data
  through native framework protocols.
- Do not maintain duplicate summarization, token-window calculation,
  checkpoint, filesystem, skills, todo, sub-agent, or vector infrastructure.
- Synchronous multi-agent execution remains testable without PostgreSQL, while
  the production default uses a persistent saver.
- The public read-only builtin directory is the Skill content source of truth;
  AppConfig/AppConfigVersion store binding references only. PostgreSQL remains
  authoritative for workspace files and memory facts.
- Every public API must enforce account/app/end-user ownership in the service
  layer.
- No backend or frontend commit until the complete acceptance matrix passes.

## Validation

- Migration/model/service/API tests for all new persistence.
- Runtime integration tests proving checkpoint continuity, workspace offload,
  builtin Skill binding/loading, fact injection, and synchronous sub-agent
  isolation.
- Existing Agent V2, memory, and service regression suites.
- Frontend type checks and production build.
- `git diff --check`, scoped staging verification, and separate backend/frontend
  commits.
