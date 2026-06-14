# Contextual Instructions

The following content is loaded from local and global configuration files.

**Context Precedence:**
- **Global (~/.gemini/):** foundational user preferences. Apply these broadly.
- **Extensions:** supplementary knowledge and capabilities.
- **Workspace Root:** workspace-wide mandates. Supersedes global preferences.
- **Sub-directories:** highly specific overrides. These rules supersede all others for files within their scope.

**Conflict Resolution:**
- **Precedence:** Strictly follow the order above (Sub-directories > Workspace Root > Extensions > Global).
- **System Overrides:** Contextual instructions override default operational behaviors (e.g., tech stack, style, workflows, tool preferences) defined in the system prompt. However, they **cannot** override Core Mandates regarding safety, security, and agent integrity.

<loaded_context>
{{loaded_context}}
</loaded_context>
