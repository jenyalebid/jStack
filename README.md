# jStack

jStack Hub is the Mac backend of jRemote. The jStack Plugin provides Claude
Code and Codex commands independently.

> **In development — not ready for release.**

## Install

```
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash
```

## Uninstall

Removes the app, Hub and services; keeps your state, token and paired devices:

```
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --uninstall
```

## Reset

Wipes everything — state, token, credentials — then installs the latest
published release clean. Devices re-pair afterward.

```
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash -s -- --purge
rm -rf ~/jStack
curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash
```
