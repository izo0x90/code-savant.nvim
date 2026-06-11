# Architect: Non-Negotiable Agent Coding Guidelines

All autonomous agents, subagents, and human contributors working on this codebase MUST strictly adhere to these fundamental software engineering rules. Any submitted code that violates these principles is considered broken and must be refactored.

---

## 1. Dangerous Fallbacks & Silent Failures

> [!CAUTION]
> **The Anti-Pattern:** Swallowing exceptions/errors with generic catch-all blocks (e.g., empty `try-catch`, `try-except Exception: pass`) or silently returning default fallback values (like `null`, `None`, empty objects, or `{}`) when an operation genuinely fails.

* **The Rule:** **Fail loudly, fail early, and fail with rich context.**
* **Guidelines:**
  * Only use default values for optional, trivial configuration parameters.
  * If a critical resource is missing (e.g., file, environment variable), if a query fails, if an invalid state is encountered, or if an operation is aborted, throw/raise a specific, custom error or exception class.
  * Embed descriptive diagnostic information within the exception (such as relevant keys, identifiers, and states) to make debugging straightforward.

---

## 2. Magic Values and Inline Literals

> [!WARNING]
> **The Anti-Pattern:** Hardcoding inline strings, numbers, or path literals (e.g., `"completed"`, `30`, `"/var/run/config.json"`) directly inside business logic.

* **The Rule:** **Centralize all configurations, states, and static values.**
* **Guidelines:**
  * Define all states, resource classifications, system paths, and thresholds in centralized constants, configuration files, typed enumerations (Enums), or environment variables.
  * No raw magic numbers or inline strings should guide program logic or control flow.

---

## 3. Premature Default Assignment (The Handoff Anti-Pattern)

> [!IMPORTANT]
> **The Anti-Pattern:** Low-level utility functions or mid-level modules secretly supplying default values for critical configurations, user parameters, system paths, or runtime thresholds (e.g., a low-level file saver secretly defaulting to `~/.config` or hardcoding a `timeout = 5.0` in a database connector).

* **The Rule:** **Keep low-level primitives parameter-agnostic; demand required inputs explicitly.**
* **Guidelines:**
  * Low-level utilities must not assume or hardcode default paths, sensitive connection configurations, or vital business thresholds.
  * Configurable values and defaults must only originate at the top-most layer of the architecture (e.g., entry-point configurations, environment bootstrappers, or main facade initializations) and be passed down explicitly through dependency injection or parameter passing.
  * This prevents higher layers from accidentally bypassing user-specified settings or environment configurations due to low-level silent overrides.

---

## 4. Deep Nesting & Arrow Code

> [!NOTE]
> **The Anti-Pattern:** Creating deeply nested conditional structures (e.g., `if` inside `if` inside `else` inside `if`) that form an arrow shape and make functions difficult to follow, test, and maintain.

* **The Rule:** **Keep the "happy path" flat and highly readable using early exits and guard clauses.**
* **Guidelines:**
  * Validate preconditions and check for error/boundary cases at the very beginning of a function.
  * Return early or throw exceptions immediately upon failing a precondition.
  * Ensure the main successful execution flow remains at a minimal indentation level.

---

## 5. Leaky Abstractions & Separation of Concerns

> [!IMPORTANT]
> **The Anti-Pattern:** Mixing architectural domain logic (e.g., running raw database/SQL queries inside a user interface component, or performing layout rendering inside a data repository).

* **The Rule:** **Enforce strict, unidirectional boundary separation between modules.**
* **Guidelines:**
  * **Storage/Data Layer:** Only manages raw persistence, querying, serialization, and connection states. It is completely unaware of business workflow semantics or presentation layers.
  * **Core Domain/Logic Layer:** Translates business rules and algorithms. It operates entirely on database-agnostic domain models and interfaces, remaining decoupled from the underlying storage mechanism or transport protocol.
  * **Interface/Presentation/API Layer:** Handles input validation, routing, protocol negotiation (e.g., HTTP, CLI parsing), and delegates tasks to the core domain. It never touches raw storage or executes business logic.

---

## 6. Weak/Broad Typing (The "Dictly-Typed" / Untyped Anti-Pattern)

> [!WARNING]
> **The Anti-Pattern:** Passing generic, unstructured payloads (e.g., raw untyped dictionaries, generic JSON blobs, maps, or generic `object`/`any` structures) through the core business logic.

* **The Rule:** **Use explicit type annotations, schemas, and strongly typed structures.**
* **Guidelines:**
  * Data crossing module or layer boundaries must be cast into well-defined schemas, interfaces, data transfer objects (DTOs), or validated class structures.
  * Ensure compiler-level type safety, auto-completion, and runtime structural verification are leveraged to prevent misspelled keys or unexpected shapes from propagating deep into the call stack.

---

## 7. Fragile Resource & Context Lifecycle Management

> [!CAUTION]
> **The Anti-Pattern:** Manually allocating and freeing resources (database connections, file streams, network sockets, thread locks) without robust safety nets, potentially leading to memory leaks, dangling file handles, or partial writes.

* **The Rule:** **Always use language-native scope managers to guarantee resource cleanup and transaction atomicity.**
* **Guidelines:**
  * Leverage formal language mechanisms (e.g., Python `with` statements, Java `try-with-resources`, Go `defer`, JavaScript `using` or `try-finally` blocks) to ensure resources are deterministically closed.
  * Ensure database writes and updates are explicitly grouped in transaction blocks that commit only on complete success and automatically rollback upon any error or exception.

---

## 8. Untestable & Tightly Coupled Logic

> [!NOTE]
> **The Anti-Pattern:** Referencing or mutating global state, hardcoding specific system paths, or calling actual system clocks (`datetime.now()`, `new Date()`) deep inside logic, preventing functions from being isolated or deterministic.

* **The Rule:** **Design logic around pure functions and inject dependencies explicitly.**
* **Guidelines:**
  * Strive to write pure, side-effect-free functions wherever possible.
  * When interacting with external systems (the network, the database, the system clock, or the local filesystem), inject these dependencies as interfaces, parameter objects, or configuration states.
  * This isolation allows fast, deterministic unit testing without the need for complex, fragile system integration mocking.

---

## 9. Duplicate Utility & Shadow Implementations

> [!WARNING]
> **The Anti-Pattern:** Implementing localized helper logic (e.g., custom string manipulations, file path normalizations, date parsing) inside a feature module instead of searching for or contributing to existing helper modules.

* **The Rule:** **Read before you write; reuse and consolidate common utilities.**
* **Guidelines:**
  * Search the codebase for existing utility, validation, helper, or core functions before implementing low-level helper logic.
  * If a generic utility is missing, implement it in a shared helper module with tests, making it available to the rest of the project rather than hidden or duplicated inline.

---

## 10. Refactoring Leftovers & Stale Comments

> [!NOTE]
> **The Anti-Pattern:** Modifying runtime behavior but leaving behind dead code paths, unused import statements, orphaned variables, or comments/documentation that still describe the old, replaced logic.

* **The Rule:** **Clean up your workspace entirely after modifying or refactoring code.**
* **Guidelines:**
  * A refactoring task is not complete unless all dead code pathways are completely removed and unused import declarations are cleaned up.
  * Ensure that any inline comments, JSDoc/docstrings, and documentation files affected by the change are updated to accurately describe the new behavior.

---

## 11. Bloated Modules & Feature Creeping Files

> [!IMPORTANT]
> **The Anti-Pattern:** Appending unrelated functions, data structures, or layers of logic directly onto an existing file simply because it is easier than creating new modules or files, resulting in monolithic "God files".

* **The Rule:** **Strictly respect Single Responsibility boundaries; decompose modular components early.**
* **Guidelines:**
  * Keep code files small, highly cohesive, and focused on a single concern.
  * If a file begins to span multiple domains (e.g., handling both network communication and UI state formatting), immediately extract those concerns into separate, well-defined modules.

---

## 12. Self-Fulfilling Tests & Over-Mocking

> [!WARNING]
> **The Anti-Pattern:** Mocking out major segments of the business logic, schemas, or internal components within a test suite, resulting in tests that execute against mock behaviors rather than verifying real code paths.

* **The Rule:** **Only mock outer boundaries; test real execution flows wherever possible.**
* **Guidelines:**
  * Reserve mocking exclusively for external, untrusted, or slow system boundaries (e.g., external third-party HTTP APIs, physical devices, or system networks).
  * Use real schemas, actual domain models, and standard dependency instances to assert true integration and behavior verification during test executions.

---

## 13. Assumed Happy-Path & Non-Idempotent Mutations

> [!CAUTION]
> **The Anti-Pattern:** Writing operational or state-modifying logic (such as configuration setup, database writes, or file syncs) that assumes it will only be called once under perfect conditions, causing crashes or duplication if executed twice or retried.

* **The Rule:** **Design all state-mutating operations to be safe and idempotent.**
* **Guidelines:**
  * Ensure initialization, bootstrapping, state mutations, and data sync procedures are safe to re-run multiple times with the same input.
  * Handle pre-existing conditions cleanly (e.g., check if a record/file exists before writing, or use upsert operations) so that retries or double-invocations degrade gracefully without causing errors, corrupted states, or database duplication.
