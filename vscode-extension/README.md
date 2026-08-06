# Be a Boss Organization for VS Code

A read-only Explorer tree for the organization served by a local be-a-boss
deployment. Docker Compose exposes the snapshot at
`http://127.0.0.1:8766/organization.json` by default.

Build a local VSIX with `npm install && npm run package`, then use VS Code's
**Extensions: Install from VSIX…** command. Change `beaboss.organizationUrl` when
the dashboard uses a non-default port.
