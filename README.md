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

## Brand

`assets/chief-of-stuff.jpeg` is the source painting (1408×768). `assets/logo.webp` is the board's
header mark, derived with `cwebp -resize 520 0 -q 62 assets/chief-of-stuff.jpeg -o assets/logo.webp`
(21 KB), and inlined as a data URI by the renderer: the board is one file, and the artifact host
serves nothing beside it. The board's palette is sampled from the painting — paper, walnut ink,
brass, sage, lavender, poppy — with Cormorant SC for headings and Alegreya Sans for text.
