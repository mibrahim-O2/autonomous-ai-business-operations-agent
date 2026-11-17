# Security Policy

## Supported versions

Only the `main` branch is supported. This is a demonstration project; it has not had an independent security audit.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"), or contact the maintainer through the GitHub account [mibrahim-O2](https://github.com/mibrahim-O2).

Include what you found, how to reproduce it, and what an attacker could do with it. You should receive an acknowledgement within seven days.

## Security model

- **Secrets** are read from environment variables only. `.env` is ignored by git; `.env.example` contains no real values.
- **Authentication and roles**: every API request is made by a principal with one tenant and one role (viewer, operator, approver, admin). Keys are hashed at rest and shown once. The tenant comes from the key, never from the request. With no key configured, the API answers only the same machine.
- **Tool access** goes through one gateway. Each agent has an allow-list of tools, arguments are validated against a schema, and the tenant's approval policy allows, denies, or holds every side-effecting call for a person. These checks run below the model, so prompt content cannot bypass them.
- **Human approval**: a held call has not executed. Approval is bound to the exact arguments shown, decided once by someone with the required role, and expires into a refusal.
- **Audit**: decisions, control actions, policy and key changes, and every side effect are written to a hash-chained log that detects edits and deletions.
- **Untrusted content** (text written by outside parties) reaches the model only in a labelled data envelope, taints the run (tightening the policy), and is scanned for injection attempts, which are shown to approvers.
- **Tenant isolation**: every stored row carries a tenant id and every query filters on it.
- **Demo integrations** act on local mock data. No real email is sent and no real CRM or calendar is contacted unless you configure a real integration yourself.

## Known limitations

- The command line is for operators with shell access to the deployment and acts as an administrator; it does not use API keys.
- SQLite data is not encrypted at rest.
- The audit chain detects changes to individual records; someone able to rewrite the whole table could rebuild it unless the head hash is kept outside the database.
- Prompt-injection detection is heuristic. The structural controls reduce risk; they do not eliminate it. Keep human approval on for consequential actions.

The full model, with what each control does not cover, is in [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md).
