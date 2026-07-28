# Progress Log

## Context Recovery

- **Task**: align the DeepAgents runtime with `docs/deepagents-v2-plan`
- **Shape**: single-full
- **Current step**: #6 — run full regression and create scoped commits
- **Status**: DONE
- **Last validation**: frontend `npm run build` passed; focused backend API/service
  tests passed; runtime focus and persistence/runtime regressions passed earlier
- **Already-corrected foundations (not committed yet)**:
  - keep native `backend=` routing; no extra FilesystemMiddleware
  - remove custom summarization wrapper and threshold/disable switches
  - load raw conversation history; let native summarization own context policy
  - publish SSE events from `EventTranslationMiddleware`, not graph chunks
- **User decision**: no staged/minimal implementation. The final design is the
  acceptance target for skills, filesystem, context, memory, summarization,
  checkpoints, multi-agent configuration, APIs, and frontend surfaces.
- **Project hard constraint**: framework-native-first. Use DeepAgents, then
  LangChain/LangGraph, then narrow adapters. Custom components require a
  documented framework gap and automated acceptance coverage. Do not keep a
  second summarizer, token-window calculator, saver, filesystem, Skills, Todo,
  sub-agent, or vector stack.
- **Acceptance matrix**: frozen in `ACCEPTANCE.md`; it covers code, DB, APIs,
  UI, tests, and scoped commit gates for every final-design subsystem.
- **Current persistence work**:
  - Skills version model/service/catalog backend implemented.
  - Workspace version service and conversation isolation implemented.
  - Memory ownership/CRUD/recall/extraction implemented.
  - The pgvector branch is rejected because the project already has Weaviate
    and DeepAgents/LangGraph provide the memory and store protocols. PostgreSQL
    remains the authoritative business fact store; vector search is an optional
    Weaviate projection only after recall evaluation proves it is needed.
- **Current V1 decisions**:
  - Skill content comes only from platform-builtin packages in a public,
    versioned, read-only directory.
  - Creator Web lists builtin Skill metadata and mounts selected Skills to the
    root agent; synchronous subagents may select only a subset of that root
    binding.
  - Remove the current database Skill content CRUD, editor, model, and
    unshipped migration from the V1 implementation.
  - Multi-agent scope is synchronous `task` delegation with
    `context_passing=NONE`; async, SUMMARY, RECENT_K, and HITL are deferred.
- **Next action**: none; all planned DeepAgents V2 alignment tasks are complete.

## 2026-07-28 Full Regression and Scoped Commits

- Backend regression passed with explicit project import path:
  `PYTHONPATH=<repo> pytest test/internal/core/agent_v2 test/internal/core/memory test/internal/handler/test_memory_fact_handler.py test/internal/handler/test_skill_handler.py test/internal/handler/test_workspace_file_handler.py test/internal/service/test_agent_runtime_service.py test/internal/service/test_memory_fact_service.py test/internal/service/test_skill_service.py test/internal/service/test_workspace_file_service.py test/internal/service/test_subagent_config.py -q`
  passed: 157 tests.
- Frontend validation passed: `npm run build`.
- `git diff --check` passed for both backend and frontend repositories; only
  line-ending warnings were emitted.
- Final prohibited-code scans found no active old Skill CRUD UI/service names;
  backend hits were limited to expected migration revision names and
  rejection-test literals for deferred context policies.
- Created scoped backend and frontend commits for the implementation work.
- Left unrelated dirty files unstaged, including IDE/Dockerfile changes,
  deleted legacy docs outside `docs/deepagents-v2-plan`, and frontend
  `.worktrees`.

## 2026-07-28 Backend APIs and Frontend Management Surfaces

- Updated Creator UI types and hooks so draft app config carries root
  `skills` bindings alongside SubAgent config.
- Replaced the old Skill content editor/create/delete flow with a read-only
  builtin Skill browser that mounts and unmounts packages by updating
  `AppConfig.skills`.
- Changed SubAgent editing from free-text Skill CSV input to a multi-select
  sourced from the root Agent's enabled Skill bindings.
- Kept Workspace file management and Memory visibility connected to the new
  resource API surface.
- Validation:
  - `npm run build` in `my-dify-ui` passed.
  - `pytest test/internal/handler/test_skill_handler.py test/internal/handler/test_workspace_file_handler.py test/internal/handler/test_memory_fact_handler.py test/internal/service/test_skill_service.py test/internal/service/test_workspace_file_service.py test/internal/service/test_memory_fact_service.py test/internal/service/test_subagent_config.py -q`
    passed: 33 tests.

## 2026-07-28 P0 Runtime Memory and Compression Cleanup

- Removed the memory pgvector dependency path from runtime code: `memory_facts`
  no longer stores an embedding column, `MemoryFactService` no longer accepts
  or searches query embeddings, and Memory CRUD/auto extraction no longer calls
  `EmbeddingsService`.
- Removed `pgvector` from `requirements.txt` and removed vector extension,
  vector column, and ivfflat index creation from `d_v2_006_finalize_memory`.
- Deleted the custom `context_window_calculator.py` and its tests; the public
  `agent_v2` exports no longer expose custom context-window or summary trigger
  helpers.
- Added `MemoryProfileRenderer` and read-only `MemoryProfileBackend`, and
  changed `AgentRuntimeService` to project PostgreSQL facts through
  `/memories/profile.md` with `memory_sources=["/memories/profile.md"]`.
- Changed `build_deep_agent` and `BaseAgent` to pass native DeepAgents
  `memory=` and explicit `permissions=`.
- Changed runtime backend assembly to use `DenyBackend` as
  `CompositeBackend.default`; workspace paths now require explicit
  `/workspace`, `/artifacts`, or `/conversation_history` namespaces.
- Kept `MyDifySandboxBackend` as a non-default development backend and updated
  its tests to use explicit `/workspace` paths.
- Validation:
  - `pytest test/internal/core/agent_v2/test_memory_profile_backend.py ... test/internal/migration/test_d_v2_alembic_chain.py -q`
    passed: 90 tests.
  - `pytest test/internal/core/agent_v2 test/internal/core/memory test/internal/handler/test_memory_fact_handler.py test/internal/handler/test_skill_handler.py test/internal/handler/test_workspace_file_handler.py test/internal/service/test_agent_runtime_service.py test/internal/service/test_memory_fact_service.py test/internal/service/test_skill_service.py test/internal/service/test_workspace_file_service.py test/internal/service/test_subagent_config.py -q`
    passed: 155 tests.
  - Prohibited-code scan found no `pgvector`, `context_window_calculator`,
    `recalled_facts`, `runtime_recalled_facts`, or `query_embedding` in the
    updated agent/runtime/memory code paths.
  - `git diff --check` passed; only line-ending warnings were emitted.

## 2026-07-28 Synchronous Multi-Agent Delegation

- Tightened SubAgent filesystem permissions to fail closed for reads as well
  as writes. Each worker now gets explicit read access only to its own
  `/subagent_skills/<name>/` source, followed by deny rules for shared
  Skill/history/memory namespaces and a final read `/**` deny.
- Kept sync-only schema validation: async modes, parent-history policies and
  legacy async fields fail validation instead of being ignored.
- Added a real DeepAgents native `task` delegation regression using a scripted
  chat model. The test verifies the parent calls `task`, the SubAgent executes,
  the parent receives the worker result, and the worker sees only the task
  description rather than parent conversation history.
- Existing event translation tests continue to map native `task` tool calls to
  `SUB_AGENT_DELEGATE` and `SUB_AGENT_END`.
- Validation:
  - `pytest test/internal/core/agent_v2/test_subagent_specs.py test/internal/core/agent_v2/test_factory.py test/internal/core/agent_v2/test_event_translation_middleware.py test/internal/service/test_agent_runtime_service.py test/internal/service/test_subagent_config.py -q`
    passed: 38 tests.
  - `pytest test/internal/core/agent_v2 test/internal/core/memory test/internal/handler/test_memory_fact_handler.py test/internal/handler/test_skill_handler.py test/internal/handler/test_workspace_file_handler.py test/internal/service/test_agent_runtime_service.py test/internal/service/test_memory_fact_service.py test/internal/service/test_skill_service.py test/internal/service/test_workspace_file_service.py test/internal/service/test_subagent_config.py -q`
    passed: 157 tests.
  - Runtime-code scan, excluding local embedding/unstructured data caches and
    ignored `__pycache__`, found no prohibited pgvector/context-window/direct
    recalled-facts/custom summarizer/token-buffer/SubagentPolicy/async-HITL
    runtime paths.
  - `git diff --check` passed; only line-ending warnings were emitted.

## 2026-07-28 DeepAgents Runtime Assembly

- Verified local DeepAgents `create_deep_agent` accepts native `backend=`,
  `skills=`, `memory=`, `permissions=`, `subagents=` and `checkpointer=`.
- Kept `build_deep_agent` as the only graph factory and confirmed it passes
  the native DeepAgents runtime parameters instead of installing custom
  Filesystem or Summarization middleware.
- Confirmed App debug, Assistant, WebApp, OpenAPI and WeChat entry paths all
  call `AgentRuntimeService.prepare` and pass the returned checkpointer,
  CompositeBackend, permissions, root Skill sources, Memory sources and
  SubAgent Skill sources into `BaseAgent`.
- Fixed runtime backend lifecycle: `BaseAgent` now closes every unique routed
  backend with `close()` after graph execution, not just
  `CompositeBackend.default`. This matters because the default is now
  fail-closed `DenyBackend`, while the workspace route owns the DB session.
- Added regression coverage for all entrypoint runtime-bundle tokens and for
  routed backend close de-duplication.
- Synchronized the filesystem permission plan with the current DeepAgents
  validation rule: permission paths must start with `/`, so the verified final
  deny pattern is `/**` plus backend-level `DenyBackend`, not a bare `*`.
- Validation:
  - `pytest test/internal/core/agent_v2/test_factory.py test/internal/core/agent_v2/test_base_agent.py test/internal/core/agent_v2/test_checkpoint_continuity.py test/internal/core/agent_v2/test_workspace_backend.py test/internal/core/agent_v2/test_skill_backend.py test/internal/core/agent_v2/test_memory_profile_backend.py test/internal/service/test_agent_runtime_service.py -q`
    passed: 69 tests.
  - `pytest test/internal/core/agent_v2 test/internal/core/memory test/internal/handler/test_memory_fact_handler.py test/internal/handler/test_skill_handler.py test/internal/handler/test_workspace_file_handler.py test/internal/service/test_agent_runtime_service.py test/internal/service/test_memory_fact_service.py test/internal/service/test_skill_service.py test/internal/service/test_workspace_file_service.py test/internal/service/test_subagent_config.py -q`
    passed: 156 tests.
  - Prohibited-code scan, excluding ignored `__pycache__`, found no
    `pgvector`, `context_window_calculator`, `recalled_facts`,
    `runtime_recalled_facts`, `query_embedding`, custom summarization module,
    token buffer, or direct `FilesystemMiddleware(...)` construction in
    `internal`, `test`, `config`, and `resources`.
  - `git diff --check` passed; only line-ending warnings were emitted.

Task #2 is complete. Skill content now comes only from public read-only builtin
packages, AppConfig/AppConfigVersion store JSONB binding references, runtime
injects enabled bindings through native DeepAgents `skills=`, and the old DB
Skill model/content CRUD plus write routes are removed.

## 2026-07-28 Builtin Skill Catalog and AppConfig Bindings

- Added `resources/agent_skills/builtin` as the platform Skill package root and
  exposed it through `BUILTIN_SKILLS_ROOT`.
- Replaced the DB-backed Skill model/service path with `SkillService` catalog
  scanning, frontmatter validation, deterministic content digests, binding
  normalization, and enabled-skill projection.
- Changed `AppConfig` and `AppConfigVersion` to persist `skills` JSONB binding
  references instead of Skill body content.
- Removed Skill create/edit/delete routes; Creator API now exposes only
  `GET /apps/<app_id>/skills` for catalog and mount-state metadata.
- Passed draft/published `skills` bindings into `AgentRuntimeService.prepare`,
  which projects selected packages via `/skills/` and subagent subsets via
  `/subagent_skills/<name>/`.
- Validation:
  - `pytest test/internal/service/test_skill_service.py test/internal/handler/test_skill_handler.py test/internal/service/test_agent_runtime_service.py test/internal/core/agent_v2/test_skill_backend.py test/internal/migration/test_d_v2_alembic_chain.py -q`
    passed: 13 tests.
  - `pytest test/internal/core/agent_v2 test/internal/core/memory test/internal/handler/test_memory_fact_handler.py test/internal/handler/test_skill_handler.py test/internal/handler/test_workspace_file_handler.py test/internal/service/test_agent_runtime_service.py test/internal/service/test_memory_fact_service.py test/internal/service/test_skill_service.py test/internal/service/test_workspace_file_service.py test/internal/service/test_subagent_config.py -q`
    passed: 154 tests.
  - Prohibited-code scan, excluding ignored `__pycache__`, found no
    `pgvector`, `context_window_calculator`, `recalled_facts`,
    `runtime_recalled_facts`, `query_embedding`, DB Skill imports, or old Skill
    mutation service names in `internal`, `test`, `config`, and `resources`.
  - `git diff --check` passed; only line-ending warnings were emitted.

## 2026-07-29 V1 Scope Consolidation

- Updated the design docs and active Taskmaster artifacts to make the Web
  builtin Skill browser/mount flow normative.
- Removed async subagents, parent-history policies, and HITL from the current
  implementation and acceptance scope.
- Kept DeepAgents-native synchronous task delegation, framework Skills
  discovery/lazy loading, and least-privilege root/subagent binding subsets.

## 2026-07-28 Context, Memory, and Compression Specification

- Added `docs/deepagents-v2-plan/03-context-memory-compression-implementation.md`
  as the complete target implementation specification.
- Frozen the layered model: LangGraph checkpoints for short-term work state,
  PostgreSQL raw messages for cold start and audit, PostgreSQL `memory_facts`
  as the long-term authority, Workspace for episodic files, and Skills for
  application capabilities.
- Frozen context assembly through native `backend=`, `skills=`, `memory=`,
  `subagents=`, and `checkpointer=` parameters.
- Frozen compression on DeepAgents `SummarizationMiddleware`, including native
  history offload and overflow recovery; custom token-window logic is rejected.
- Documented deletion semantics, phased implementation order, test matrix, and
  final merge gates. Task #2 remains IN_PROGRESS because this update defines
  the target but does not yet change runtime code.
