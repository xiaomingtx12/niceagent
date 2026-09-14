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
