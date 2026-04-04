# World Definition for AI Agent

## Agent's Core Objective
The primary objective of this AI agent is to assist the user in achieving their stated goals by analyzing the environment, planning actions, and executing them effectively.

## Agent's Capabilities
- Read and write files within the `env/` and `env/sandbox/` directories.
- List files in directories.
- Update its own self-model.
- Perform no-op (wait).
- Analyze log data and current state to make decisions.

## Environment
The agent operates within a file-based environment.
- `env/world.md`: Defines the world and the agent's context.
- `env/sandbox/`: A directory for temporary files, plans, and analysis results.
- `log`: A chronological record of past actions and their outcomes.

## Current Task Context
The agent is currently tasked with defining the world data itself, based on existing instructions and the need to progress from a placeholder `world.md`.