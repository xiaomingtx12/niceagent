# NiceAgent Domain Language

NiceAgent distinguishes authenticated platform identities from application-scoped consumers. These terms define conversation identity and ownership consistently across entry points.

## Language

**Account**:
An authenticated platform identity that can own and manage platform resources.
_Avoid_: User, End User

**End User**:
An application-scoped consumer that interacts through WebApp, Service API, or another external channel. Its optional linked Account identifies the consumer, while the application's owner remains a separate role.
_Avoid_: Account, platform user

**Application Owner**:
The Account that owns and configures an application. This can differ from the Account consuming the application or owning a conversation in it.
_Avoid_: End User, Conversation Owner

**Conversation Owner**:
The Account authorized to access and manage a conversation. Ownership remains stable regardless of which entry point created later messages.
_Avoid_: Creator, End User

**Conversation Creator**:
The Account or End User that originated a conversation. This records origin and is not the canonical authorization identity.
_Avoid_: Owner

**Run**:
A user's logical Agent task, retaining its identity while waiting for answers and resuming execution.
_Avoid_: Execution thread, SSE connection

**Conversation**:
A continuing exchange that groups a participant's Agent tasks under stable ownership. Starting another task in the conversation does not create a new conversation.
_Avoid_: Run, network connection

**Message**:
One round of a user's question, the Agent's answer, and the usage accumulated while answering it. Providing requested information continues the same round.
_Avoid_: Stream fragment, individual model message

**Agent Definition**:
The effective instructions and capabilities of an Agent for a particular entry point. A draft definition, a published definition, and the built-in assistant definition have different sources.
_Avoid_: Run, model connection

**Input Batch**:
The complete set of pending forms that must be answered together. Reopening cancelled forms preserves the same batch.
_Avoid_: Answer hash, individual field

**Submission**:
The accepted answers to one Input Batch. Repeating those answers returns the original acceptance; changing accepted answers is rejected.
_Avoid_: Pending input, Run

**Agent Evaluation**:
An independent assessment using captured task material and explicit criteria. Reassessing a task creates another assessment without changing the original conversation.
_Avoid_: Run status, production task replay
