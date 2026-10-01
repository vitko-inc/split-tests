# Split tests

A GitHub Action for [Vitko Runners](https://runners.vitko.inc). It runs your test command as
several parts at the same time. Each part runs in its own copy of your job, and the results come
back to the original job: one log, one list of failures, one pass or fail.

The copies start from the moment the step begins. Your checkout, dependencies and build are
already in them, so nothing is installed or built twice. You add one step and change nothing
else.

On any other runner (GitHub-hosted, your own machines) the step simply runs all your tests, as a
plain `run:` step would, so the same workflow works everywhere.

## Add it to your workflow

Replace your test step with the `vitko-inc/split-tests` action, and put your test command in
`run`.

**pytest**

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    run: pytest -q tests/
```

**cargo nextest** (build first, in the same job or in this step: the parts share the build)

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    run: cargo nextest run --workspace --no-fail-fast
```

**Jest**

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    run: npx jest --ci
```

**Vitest**

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    run: npx vitest run
```

**Go**

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    run: go test ./...
```

**Anything else.** Set `tool: command`. Your command then runs in every part with two
environment variables, `VITKO_PART` (1, 2, …) and `VITKO_PARTS` (how many), and it chooses its
own share of the tests. Most test tools have an option for this.

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    tool: command
    run: ./scripts/run-tests.sh --part "$VITKO_PART" --of "$VITKO_PARTS"
```

Put a single test command in `run`. If `run` is a shell script (with `&&`, pipes, variables or
several lines), the tests run in one part, and the log says so. Use `working-directory` to run
somewhere else.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `run` | (required) | Your test command. |
| `parts` | `auto` | How many parts. `auto` picks a number that suits your plan and the size of your test suite: a suite estimated at under a minute runs in one part, because splitting it would cost more time and money than it saves. |
| `tool` | `auto` | `pytest`, `nextest`, `jest`, `vitest`, `go` or `command`. `auto` works it out from `run`. |
| `env` | | Extra environment variables your tests need, by name. |
| `junit` | `vitko-split-tests.xml` | Where to write a JUnit report of every test. Empty to skip it. |
| `working-directory` | `.` | Where to run the tests. |
| `timings-file` | | Optional. A file with test durations, if you keep one. Vitko Runners keeps them for you. |

Outputs: `junit` (the report's path), `parts` (how many ran) and `failed` (how many tests failed).
When the tests run unsplit (on a runner that can't split them), `parts` is `1` and there is no report: `junit` and `failed` are empty.

## How the tests are shared out

- **pytest** collects your tests once, then each part runs its share. If you use pytest-xdist's
  `-n`, each part also runs that many tests at a time.
- **cargo nextest** lists your tests once, then each part runs its share.
- **Go** shares out packages.
- **Jest** and **Vitest** share out test files, the way their own `--shard` option does.

After the first run, Vitko Runners remembers how long each test took and balances the parts by
time, so they finish close together.

## The environment your tests see

The parts don't get the step's whole environment, because it can hold tokens. They get the
usual build and toolchain variables (`PATH`, `HOME`, `CI`, `GITHUB_SHA`, `CARGO_*`, `RUSTFLAGS`,
`PYTHONPATH`, `NODE_OPTIONS`, `GO*` and similar), plus any names you list in `env`:

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    run: pytest -q
    env: DATABASE_URL FEATURE_FLAGS
```

Names that look like secrets (containing `TOKEN`, `SECRET`, `PASSWORD`, `PRIVATE`, `CREDENTIAL`,
or ending in `_KEY`) are never passed, even if you list them. The log says when that happens.

The parts have no network access, and the original job is the only one that talks to GitHub. If
your tests need a service such as a database, start it before this step: each part gets its own
copy of it, already running.

## What you see in the job

- A line as each part starts and finishes.
- Each part's full output, in a section you can expand.
- Every failed test with its output, in one place below the parts.
- Failed tests as annotations on the run, up to 10.
- A table of parts and results in the run's summary.
- A JUnit report of every test, for test-report actions.

The step fails if any test fails, if a part stops early, or if any test doesn't report a
result. It never passes on partial results.

## On other runners

On GitHub-hosted runners, your own machines, or anywhere without Vitko Runners, the step runs
your command once, with all your tests, exactly as a plain `run:` step would. The log says
"Running all tests in this job". You can keep the step in workflows that run in both places.

## Billing

You pay for the time the copies run. The original job's wait while its copies run isn't
charged.

## Limits

- Up to your plan's limit of parts at once. With `auto`, fewer parts when the suite is short, and one part when it's estimated at under a minute (from past timings, or from the number of tests the first time). The log says so; set `parts` to a number to split anyway.
- One split-tests step at a time in a job, and up to four in one job.
- The parts can't reach the network, so tests that download things at run time fail in parts.
  Fetch what they need in an earlier step.
- Tests that depend on running in a fixed order, or on each other, can fail when split, as they
  can with any parallel test runner.

## Versions

Use `vitko-inc/split-tests@v1`. The `v1` tag moves to the latest compatible release; pin a
release such as `@v1.0.0`, or a commit, if you prefer.

## License

Apache License 2.0. See [LICENSE](LICENSE).
