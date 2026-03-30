# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| main    | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT** open a public GitHub issue
2. [Open a private security advisory](https://github.com/yangwhale/CloseCrab/security/advisories/new) on GitHub
3. Include steps to reproduce if possible
4. We will acknowledge receipt within 48 hours

## Security Considerations

CloseCrab bridges chat platforms with Claude Code CLI, which has full shell access. Operators should:

- **Whitelist users** — Only authorized user IDs in bot config (Firestore `auth.allowed_users`)
- **Use environment variables** for all secrets (`.env` file, never commit)
- **Review skills** before deploying — skills can execute arbitrary commands
- **Run in isolated environments** — Use dedicated VMs or containers
- **Monitor bot activity** — Check Firestore conversation logs and Discord log channels
