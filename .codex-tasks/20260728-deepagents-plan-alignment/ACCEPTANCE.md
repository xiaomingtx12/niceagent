# Final Design Acceptance Matrix

| Subsystem | Persistence | Runtime | API and permissions | Frontend | Tests |
|---|---|---|---|---|---|
| Skills | Public versioned read-only builtin directory is the content source; AppConfig/AppConfigVersion store name/version/digest bindings | Only root-bound Skills are projected under `/skills`; each synchronous subagent receives an explicit subset through native `skills=` and CompositeBackend | Creator can list builtin metadata and save bindings; no content create/edit/upload/delete API | Creator builtin Skill browser with root/subagent mount state; no editor | catalog schema/digest, binding validation, isolation, native SkillsMiddleware load |
| Workspace | `workspace_files` versioned rows, seed/conversation scope, soft delete | `MyDifyWorkspaceBackend` injected at every entry; virtual root supports `/conversation_history`; native Filesystem/Summarization share backend | Creator all app files; end-user only own conversation; upload/read/write/delete/list | Creator workspace browser; WebApp conversation-history panel | traversal/isolation/version/API/offload |
| Context | App/AppConfig/Account/EndUser facts remain authoritative | L1 stable identity, native L2 skills, dynamic L3 runtime/facts, L4 checkpoint messages; no giant legacy template | Inputs resolved server-side, never trusted from client | Configuration labels describe real behavior | prompt stability/dynamic refresh/no duplication |
| Memory | PostgreSQL `memory_facts` is the authoritative business store; no pgvector column; optional Weaviate index is a rebuildable projection | DeepAgents `memory=` and backend expose a read-only user profile; recall falls back to PostgreSQL and revalidates every projected fact | End-user list/add/edit/delete/batch-delete; creator read-only | Creator read-only memory view; end-user My Memory page | isolation/CRUD/recall/extraction/projection fallback |
| Compression | No custom summarizer, token-buffer pre-trim, or context-window calculator | Native DeepAgents SummarizationMiddleware with DB workspace offload; original history and checkpoint state remain readable | History files obey workspace permissions | History file visible in workspace panels | native trigger/offload/overflow/reload regression |
| Checkpoint | Native LangGraph saver tables only | configurable memory/postgres/disabled mode; production persistent default; stable thread id; DB history only bootstraps an empty thread | no public mutation API | no fake toggle | two-run continuity and no message duplication |
| Multi-agent | final list-shaped JSON config in app config/version | native synchronous subagents only; explicit tool/Skill subsets; `context_passing=NONE`; single-level `task` delegation | save/publish rejects async, SUMMARY, RECENT_K and unknown capability references | synchronous team editor only; no async or HITL controls | schema conversion, runtime payload, isolation, real-model sync delegation |
| Events | existing message/thought persistence retained | EventTranslationMiddleware is the only SSE boundary; no reviewed-output leak; subagent metadata preserved | existing SSE names unchanged | execution trace remains compatible | model/tool/subagent/error/single-terminal |
| Invocation coverage | n/a | App, assistant, WebApp, OpenAPI, WeChat and workflow Agent entry paths use the same runtime builder | existing auth remains intact | existing chat flows work | service coverage and smoke tests |

## Commit Gate

- No placeholder comments claiming later phases for an in-scope feature.
- No inactive handler/service/backend that is never registered or injected.
- No creator write access to end-user facts.
- No client-supplied end-user identity without server-side ownership resolution.
- No custom implementation duplicates a DeepAgents/LangChain/LangGraph native
  component without a documented framework gap and acceptance test.
- No unrelated files staged.
