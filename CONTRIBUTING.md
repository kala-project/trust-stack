# Contributing

TrustStack handles evidence, model execution, and immutable publication records. Changes should preserve those boundaries rather than adding shortcuts that make a result look more trustworthy than it is.

## Before opening a change

1. Keep credentials, downloaded documents, databases, reports, and local runtime state out of Git.
2. Use existing contracts and storage patterns before adding a new abstraction.
3. Preserve Chinese and other source-language report content; product chrome and public documentation are English.
4. Do not weaken SSRF, source identity, content hash, immutable version, or review-status controls to make a test pass.
5. Add a focused regression test for changed behavior.

## Development setup

Follow [Getting Started](docs/getting-started.md), then run:

```sh
make check
```

Optional Python type checks require an isolated environment populated from `requirements-dev.lock`:

```sh
make python-typecheck
```

Live-model and fresh-network checks are intentionally separate from deterministic tests. State clearly when a change has not been exercised with a real model or a fresh source download.

## Pull requests

Keep changes focused. Describe:

- the user-visible problem;
- the trust or security boundary affected;
- the verification performed;
- remaining limitations or untested paths;
- any schema, API, source-rights, or immutable-version implications.

Do not use generated fixtures, old model answers, or surrogate output as evidence of a successful live-model acceptance run.
