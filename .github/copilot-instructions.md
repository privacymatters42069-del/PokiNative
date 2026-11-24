# GitHub Copilot Instructions

## Language & Communication
- Always respond in English, regardless of the language used in repository files, comments, or commit history.
- Keep explanations concise, direct, and focused on actionable code changes.

## Project Context
- **Overview:** [Briefly describe what this project does in 1–2 sentences]
- **Tech Stack:** [e.g., Python 3.12, C++, GitHub Actions, etc.]
- **Build / Test Commands:**
  - Build: `[e.g., cmake --build build]`
  - Test: `[e.g., pytest]`

## Coding Standards & Conventions
- Follow existing code formatting, naming conventions, and directory structure in this repository.
- Prefer clear, maintainable code over clever one-liners.
- Include brief comments only where logic is non-obvious.
- Do not modify build workflows, release scripts, or configuration files unless explicitly asked.

## File & Repository Hygiene
- Never commit build artifacts, temporary files, or cached bytecode (e.g., `__pycache__/`, `*.pyc`, `build/`).
- Keep documentation and code comments in English unless a file explicitly requires localization.
