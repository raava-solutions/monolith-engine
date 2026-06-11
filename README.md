# monolith-engine

The open single-node engine for Monolith. Runs core agent-infrastructure
primitives (container provider, local storage, chat-exec) **in-process with
no server**. The open CLI imports it for local; the closed hosted service
imports the same engine for cloud, supplying its own provider/storage/secrets
backends behind identical ports.

Apache-2.0.
