# 2026-09-03 Project description context

Root cause: Project creation persisted `description`, but Session binding and `ProjectContext` froze only name, workspace, and instructions. The Agent therefore saw the Project name and instruction code but guessed the description.

The minimal fix adds a backward-compatible `session_projects.description` snapshot, carries it through `ProjectContext`, and labels it in the existing ephemeral Project block. Existing Sessions retain an empty description snapshot; new Sessions freeze the current value. Targeted tests passed 240/240, and local real WebUI/Run Broker UAT passed five consecutive turns in a fresh Session. Upstream `hermes-agent` is unchanged.
