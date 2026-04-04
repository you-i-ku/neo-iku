# Task Breakdown and Analysis Plan based on World Definition

## Objective
To analyze the defined world (env/world.md) and current environment to identify concrete, actionable tasks that move towards the user's ultimate goals.

## Steps:

1.  **Review `env/world.md`:** Re-read the world definition to ensure full understanding of the agent's objective, capabilities, and environment context.
2.  **Identify Current State and Goal:** Based on the current log and `self` model, determine the immediate next logical step or the current sub-goal.
3.  **Analyze Environment (`env/` directory):**
    *   List files in `env/` and `env/sandbox/` to understand available resources and existing work.
    *   Read relevant files (e.g., `analysis_plan.md`, `plan.md`, `test_file.txt` if applicable) for further context or instructions.
4.  **Task Decomposition:**
    *   Break down the identified goal into smaller, manageable sub-tasks.
    *   Each sub-task should be specific, measurable, achievable, relevant, and time-bound (SMART criteria).
    *   Consider the agent's capabilities when defining tasks.
5.  **Action Planning:**
    *   For each sub-task, determine the appropriate tool (`list_files`, `read_file`, `act_on_env`, `update_self`, `wait`).
    *   Formulate the arguments for the chosen tool.
    *   Define the `intent` and `expect` for each action.
6.  **Update Self-Model:** After completing significant tasks or phases, update `self[current_phase]` to reflect progress (e.g., from `world_data_definition_in_progress` to `task_planning_in_progress`).

## Expected Outcome
A clear sequence of actions (tool calls) that, when executed, will lead to progress towards the user's goal, documented in a new plan or by updating existing plan files.