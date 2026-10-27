# nexus-crew

The Nexus crew's souls: one SOUL.md + IDENTITY.md per agent (main/Nexus,
architect, angel, sleuth, tape, canvas, switchboard, lens, vault, omerta).

Install on the Nexus box:

```bash
git pull
bash nexus-crew/install-crew.sh
```

The installer backs up each stock file once (`*.stock-backup`), copies the
crew files into `~/.openclaw/workspaces/<agent>/` (main lives at
`~/.openclaw/workspace`), and restarts the gateway.
