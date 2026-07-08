# Workflow orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately – don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Research Mode Default
- Always use web search to verify about the latest updates and truth

### 3. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution
- If subagents need to share some context use agent team
- Utilize agent team whenever possible for efficient work

### 4. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 5. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 6. Continuously improve the work
- Even after doing the first verification create review agent and ask for improvements.

### 7. Final documentation
- Always document the feature of the codebase and how it works in details in a Markdown. Create a subagent to do this. Think of it like a secretary. If such Markdown already exist command that subagent to always update the Markdown.
