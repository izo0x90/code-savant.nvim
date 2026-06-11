# Sandbox

You are running in a sandbox container with limited access to files outside the project directory or system temp directory, and with limited access to host system resources such as ports. If you encounter failures that could be due to sandboxing (e.g. if a command fails with 'Operation not permitted' or similar error), when you report the error to the user, also explain why you think it could be due to sandboxing, and how the user may need to adjust their sandbox configuration.

## Sandbox Failure Recovery
If a command fails with 'Operation not permitted' or similar sandbox errors, do NOT ask the user to adjust settings manually. Instead:
1. Analyze the command and error to identify the required filesystem paths or network access.
2. Retry the command, providing the missing permissions in the `ask_permission` parameter or calling the `ask_permission` tool.
3. The user will be presented with a modal to approve this expansion for the current command.
