# chief-of-stuff

A Claude Code agent that runs as the main session and coordinates a working day. It does not do the work. It keeps the clock, the daily log, and the list of who owns each open item, and it proposes subagent launches for Zach to approve.

**Status:** layout only. No agent file, no evals yet. Nothing below has been run.

## Launch

```sh
claude --agent chief-of-stuff
```

## Install (when built)

```sh
claude plugin marketplace add ~/Developer/chief-of-stuff
claude plugin install chief-of-stuff@chief-of-stuff
```
