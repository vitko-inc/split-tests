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
own share of the tests. Most test tools have an option for this. If the command doesn't use
`VITKO_PART` or `VITKO_PARTS`, `parts: auto` runs it once, since every part would otherwise run
all of it.

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    tool: command
    run: ./scripts/run-tests.sh --part "$VITKO_PART" --of "$VITKO_PARTS"
```

Run the test tool itself in `run` (for example `uv run pytest`, not a wrapper such as `tox` or
`make test`), so the action can see the tests and share them out. Put a single test command in `run`. If `run` is a shell script (with `&&`, pipes, variables or
several lines), the tests run in one part, and the log says so. Use `working-directory` to run
somewhere else.

## Install and build before the split

The copies are made when the tests start, and **they can't reach the network**. Everything your
tests need must be installed and built before that: in an earlier step, or in `prepare`, which
runs in this job first, with network:

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    prepare: uv sync --locked
    run: uv run --locked pytest -q
```

A command that installs or builds as it starts (for example `tox`, or `uv run` with an
environment that isn't synced yet) does that in every part, and fails there because the parts
can't download anything.

## Tests that need the network

Tests that download things or call outside services can't run in the parts. Mark them, leave
them out of the split, and run them in a separate step:

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    run: pytest -q -m "not network"
- run: pytest -q -m network
```

With Go, `go test -skip 'TestLive|TestIntegration' ./...` in the split and
`go test -run 'TestLive|TestIntegration' ./...` in a separate step does the same. With Jest or
Vitest, use a separate config or a file-name pattern for the network tests.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `run` | (required) | Your test command. |
| `prepare` | | A command to run first, in this job, with network: install dependencies and build. |
| `parts` | `auto` | How many parts. `auto` chooses from how long this step took before, by `optimize` (see [How many parts](#how-many-parts)). A number always splits into that many parts, up to your plan's limit. |
| `optimize` | `balanced` | With `parts: auto`: `cost`, `balanced` or `speed`. |
| `wait-for-capacity` | `90` | When the runner has no room to split right now, how many seconds to keep asking before running the tests unsplit. `0` doesn't wait. |
| `tool` | `auto` | `pytest`, `nextest`, `jest`, `vitest`, `go` or `command`. `auto` works it out from `run`. |
| `env` | | Extra environment variables your tests need, by name. |
| `junit` | `vitko-split-tests.xml` | Where to write a JUnit report of every test. Empty to skip it. |
| `working-directory` | `.` | Where to run the tests. |
| `timings-file` | | Optional. A file with test durations, if you keep one. Vitko Runners keeps them for you. |

Outputs: `junit` (the report's path), `parts` (how many ran), `failed` (how many tests failed),
`unsplit-reason` (why the tests ran in one part; empty when they were split), `parts-requested`
and `parts-allowed`.
When the tests run unsplit (on a runner that can't split them), `parts` is `1` and there is no report: `junit` and `failed` are empty.

## How many parts

Every part is a copy of your job, and you pay for the time each copy runs. Each copy also repeats
some fixed work before its first test: the test tool starts, loads your code and, for tools that
compile or transform it (TypeScript, for example), does that again. So splitting buys a shorter
wait with extra billed time, and for some suites the extra time is larger than the wait it saves.

With `parts: auto`, the step keeps a short history of its own runs: how many parts, how long the
copies ran in total, and how long the step took. From it, it estimates the step's time in one part
and the extra time each part adds, and chooses the number of parts by `optimize`:

| `optimize` | Splits when |
|---|---|
| `cost` | Splitting costs no more than running in one part. |
| `balanced` (default) | Every extra billed second saves at least one second of waiting. |
| `speed` | It shortens the wait, accepting up to 10 billed seconds per second saved. |

When splitting would cost more than it saves, the tests run in one part and the log says so, with
the estimates. If your suite's tests have little fixed work per part, splitting can even cost
less than one part, and every setting splits.

The first two runs of a step measure: the first runs in one part, the second in two parts. From
the third run on, the step chooses by cost. As your suite grows, the history follows it, and a step that stopped splitting starts
again once splitting pays off. With `optimize: speed`, the step splits from the first run, as it
would by the suite's size, and uses the history once it has one.

```yaml
- uses: vitko-inc/split-tests@v1
  with:
    run: npx jest --ci
    optimize: speed   # or cost
```

Set `parts` to a number to choose yourself.

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

## When the tests run unsplit

Sometimes the tests run in one part, in this job: the runner has no room to make copies right now,
the run isn't on Vitko Runners, the suite is too short to gain from splitting, or the command
can't split itself. The step still runs every test and passes or fails on them. It also says why,
where you'll see it: a notice on the run, a section in the run's summary (with the parts asked
for and allowed), and the `unsplit-reason` output:

| `unsplit-reason` | Meaning |
|---|---|
| `host-busy` | The runner had no room to split, even after `wait-for-capacity` seconds. |
| `not-vitko` | Not running on Vitko Runners. |
| `setup` | The job couldn't start the helper that makes copies (it needs passwordless `sudo`). |
| `host-error` | The runner couldn't make copies of this job. |
| `turned-off` | Splitting was turned off. |
| `shell-script` | `run` is a shell script, not a single test command. |
| `short-suite` | `parts: auto` chose one part: the suite takes under a minute. |
| `command-not-split` | `parts: auto` chose one part: a `tool: command` command that doesn't use `VITKO_PART`. |
| `learning` | `parts: auto` ran in one part to measure the step's time in one part (the step's first run). |
| `costs-more` | `parts: auto` chose one part: by this step's history, splitting would cost more than it saves for your `optimize` setting. |

When the runner is busy, the step asks again for up to `wait-for-capacity` seconds (90 by
default), with pauses that grow from 5 to 30 seconds, before running the tests unsplit.

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
charged. Since each copy repeats some start-up work, a split usually costs somewhat more in total
than one part; `parts: auto` weighs that against the time it saves (see
[How many parts](#how-many-parts)).

## Limits

- Up to your plan's limit of parts at once. With `auto`, the number of parts that `optimize` favours, and one part when the suite is short or splitting would cost more than it saves. The log says so; set `parts` to a number to split anyway.
- One split-tests step at a time in a job, and up to four in one job.
- The parts can't reach the network: install and build before the split (see above), and run
  tests that need the network in a separate step.
- Tests that depend on running in a fixed order, or on each other, can fail when split, as they
  can with any parallel test runner.
- Jest and Vitest give each part whole test files, as their own `--shard` does. A run can't be
  shorter than its longest file, so one long file limits the gain; the summary says when that
  happens. Splitting the file into smaller files helps.

## Versions

Use `vitko-inc/split-tests@v1`. The `v1` tag moves to the latest compatible release; pin a
release such as `@v1.0.0`, or a commit, if you prefer.

## License

Apache License 2.0. See [LICENSE](LICENSE).
