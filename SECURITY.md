# Security Policy

## Supported versions

Only the `main` branch is supported. This is a demonstration project; it has not had an independent security audit.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"), or contact the maintainer through the GitHub account [mibrahim-O2](https://github.com/mibrahim-O2).

Include what you found, how to reproduce it, and what an attacker could do with it. You should receive an acknowledgement within seven days.

## Security model

- **Secrets** are read from environment variables only. `.env` is ignored by git; `.env.example` contains no real values.
- **Tool access** goes through one gateway. Each agent has an allow-list of tools, arguments are validated against a schema, and an approval gate can allow, deny, or suspend any call. These checks run below the model, so prompt content cannot bypass them.
- **Untrusted content** (text written by outside parties) is flagged on tool results and taints the run. Agents are instructed to treat tool output as data.
- **Tenant isolation**: every stored row carries a tenant id and every query filters on it.
- **Demo integrations** act on local mock data. No real email is sent and no real CRM or calendar is contacted unless you configure a real integration yourself.

## Known limitations

- The built-in command line has no authentication; anyone who can run it can act as any tenant. Authentication and role-based access are added with the API layer.
- SQLite data is not encrypted at rest.
- Prompt-injection defences reduce risk; they do not eliminate it. Keep human approval on for consequential actions.
