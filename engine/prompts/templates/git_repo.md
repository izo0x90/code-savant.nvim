# Git Repository

- The current working (project) directory is being managed by a git repository.
- **NEVER** stage or commit your changes, unless you are explicitly instructed to commit. For example:
  - "Commit the change" -> add changed files and commit.
  - "Wrap up this PR for me" -> do not commit.
- When asked to commit changes or prepare a commit, always start by gathering information using shell commands:
  - `git status` to ensure that all relevant files are tracked and staged, using `git add <file>...` for specific files as needed.
  - `git diff HEAD` to review all changes (including unstaged changes) to tracked files in work tree since last commit.
    - `git diff --staged` to review only staged changes when a partial commit makes sense or was requested by the user.
  - `git log -n 3` to review recent commit messages and match their style (verbosity, formatting, signature line, etc.)
- Do not use `git add .` or `git add -A` unprompted as this can stage unwanted or untracked files. Instead, stage only the specific files that were changed or created as part of the task.
- Combine shell commands whenever possible to save time/steps, e.g. `git status && git diff HEAD && git log -n 3`.
- Always propose a draft commit message. Never just ask the user to give you the full commit message.
- Prefer commit messages that are clear, concise, and focused more on "why" and less on "what".
- After each commit, confirm that it was successful by running `git status`.
- If a commit fails, never attempt to work around the issues without being asked to do so.
- Never push changes to a remote repository without being asked explicitly by the user.
