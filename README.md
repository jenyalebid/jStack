# jStack

> **Beta — under construction. Not ready for release.**

jStack Plugin provides Claude Code and Codex commands independently.
jStack Hub is the Mac server and menu bar; jRemote is its separately installed
client. A parent Hub manages leaves, each running one installation of the same Hub.

[Product contract](product.md#core-contract) ·
[Build, release and update workflow](docs/managed-updates.md)

Develop on branches; main holds complete, verified functionality. Publishing
is an explicit action over tested artifacts, never a side effect of a push.

## Install

```
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash
```

## Uninstall

```
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --uninstall
```
