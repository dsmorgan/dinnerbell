# Contributing to Dinnerbell

Thanks for helping. Dinnerbell is licensed under [AGPL-3.0-or-later](LICENSE), and every contribution is accepted under the same license.

## Sign your commits (Developer Certificate of Origin)

Every commit must carry a `Signed-off-by` line. By adding it, you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the change, or otherwise have the right to submit it under the project's license.

Add the line automatically with `-s`:

```sh
git commit -s -m "Explain what changed and why"
```

The line uses your configured `user.name` and `user.email`:

```
Signed-off-by: Your Name <you@example.com>
```

To sign off commits you already made on a branch, run `git rebase --signoff main`.

## Set up

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
```

## Before you open a pull request

Run the same checks CI runs:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov
docker build -f Containerfile -t dinnerbell .
```

[CLAUDE.md](CLAUDE.md) lists the project conventions: layers and import rules, license headers, and the migration workflow. They apply to people and agents alike.

## Report a security problem

Don't open a public issue. Follow [SECURITY.md](SECURITY.md).
