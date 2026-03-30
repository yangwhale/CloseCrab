# Contributing to CloseCrab

Thank you for your interest in contributing to CloseCrab! This document provides guidelines for contributing.

## How to Contribute

### Reporting Issues

- Use [GitHub Issues](https://github.com/yangwhale/CloseCrab/issues) to report bugs or request features
- Include steps to reproduce, expected vs actual behavior, and environment details
- For security vulnerabilities, see [SECURITY.md](SECURITY.md) instead

### Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Test your changes locally with `./run.sh`
5. Commit with clear messages: `git commit -m "feat: add new channel adapter"`
6. Push and open a PR against `main`

### Commit Message Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` — New feature
- `fix:` — Bug fix
- `docs:` — Documentation only
- `refactor:` — Code change that neither fixes a bug nor adds a feature
- `test:` — Adding or updating tests
- `chore:` — Maintenance tasks

### Code Style

- Python: Follow PEP 8, use type hints where practical
- Shell: Use `set -euo pipefail`, quote variables
- Keep files focused — one module, one responsibility
- No unnecessary abstractions for one-time operations

### Adding a New Skill

1. Create `skills/<skill-name>/SKILL.md` with YAML frontmatter
2. Add scripts to `skills/<skill-name>/scripts/` if needed
3. Follow existing skills as examples (see `skills/skill-creator/`)
4. Test locally before submitting

### Adding a New Channel Adapter

1. Implement the `ChannelBase` interface in `closecrab/channels/`
2. Handle message normalization to `UnifiedMessage`
3. Support progress callbacks and message chunking
4. Add platform-specific configuration to `.env.example`

## Development Setup

```bash
# Clone
git clone https://github.com/yangwhale/CloseCrab.git
cd CloseCrab

# Configure
cp .env.example .env
# Edit .env with your tokens

# Deploy Claude Code + skills
./deploy.sh

# Run
./run.sh --bot my-bot
```

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
