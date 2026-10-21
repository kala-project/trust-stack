# TrustStack

## The Problem

Agents based on modern LLMs are quite powerful, but they still make mistakes. In high-stakes fields such as financial research and investment, a plausible but incorrect answer can lead to significant financial loss.

TrustStack starts from a fundamental assumption: no matter how carefully an LLM-based system is designed, errors are inevitable. The goal is not to pretend that an agent is always correct, but to reduce the likelihood of error and make errors easier for humans to find.

## My Solution

TrustStack addresses this problem through two complementary designs.

First, TrustStack is designed as a layered system. Each layer has a defined responsibility, and the boundaries and handoffs between layers are explicit. Together, these layers reduce the likelihood that an error passes unnoticed from source collection to final publication.

Second, the system preserves the path from a published conclusion back through its reasoning, calculations, data, and original sources. When an error occurs, a human reviewer can inspect that path, understand how the result was produced, and make the final judgment.

The layered system is designed to prevent and expose errors before publication. Traceability makes the errors that remain easier for humans to investigate and correct.

## Documentation

- [Getting Started](docs/getting-started.md)
- [System Architecture](docs/architecture.md)
- [Public Test Data](docs/public-test-data.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [License](LICENSE)
