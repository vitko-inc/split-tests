"""The declared environment: what the tests in each part may see.

Parts never inherit the step's environment, which can hold tokens. They get a fixed allowlist of
build and toolchain variables plus the names the workflow lists with ``env``. Names that look like
credentials are refused even when listed."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping, Tuple
from urllib.parse import urlsplit

ALLOWED = frozenset(
    """
PATH HOME USER LOGNAME SHELL LANG LANGUAGE TERM TZ TMPDIR
CI GITHUB_ACTIONS GITHUB_WORKSPACE GITHUB_REPOSITORY GITHUB_REPOSITORY_OWNER GITHUB_SHA GITHUB_REF
GITHUB_REF_NAME GITHUB_HEAD_REF GITHUB_BASE_REF GITHUB_RUN_ID GITHUB_RUN_NUMBER GITHUB_RUN_ATTEMPT
GITHUB_JOB GITHUB_WORKFLOW GITHUB_EVENT_NAME GITHUB_SERVER_URL
RUNNER_OS RUNNER_ARCH RUNNER_TEMP RUNNER_TOOL_CACHE ImageOS ImageVersion
CARGO_HOME RUSTUP_HOME RUSTUP_TOOLCHAIN CARGO_TARGET_DIR CARGO_TERM_COLOR CARGO_INCREMENTAL
CARGO_BUILD_JOBS CARGO_NET_OFFLINE RUSTFLAGS RUSTDOCFLAGS RUST_BACKTRACE RUST_MIN_STACK RUSTC_WRAPPER
NEXTEST_PROFILE NEXTEST_RETRIES NEXTEST_TEST_THREADS NEXTEST_HIDE_PROGRESS_BAR
CC CXX AR CFLAGS CXXFLAGS CPPFLAGS LDFLAGS PKG_CONFIG_PATH LD_LIBRARY_PATH
VIRTUAL_ENV CONDA_PREFIX PYTHONPATH PYTHONHASHSEED PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED
PYTHONWARNINGS PYTHONIOENCODING PYTEST_ADDOPTS
NODE_OPTIONS NODE_ENV NODE_PATH
GOPATH GOROOT GOFLAGS GOCACHE GOMODCACHE GOTOOLCHAIN GOOS GOARCH CGO_ENABLED GO111MODULE GOPROXY
GONOSUMDB GOPRIVATE
JAVA_HOME DOTNET_ROOT
""".split()
)
ALLOWED_PREFIXES = ("LC_",)
PROXY_NAMES = frozenset(["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"])
# The in-job helper applies the same rule to each part's environment before it runs the part
# (helper.py, secret_env_count); tests/test_envpolicy.py keeps the two identical.
REFUSED = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|_KEY$|^ACTIONS_", re.IGNORECASE)
# Names that match REFUSED but are settings, not credentials.
BENIGN = frozenset(["TOKENIZERS_PARALLELISM"])


def refused(name: str) -> bool:
    return name not in BENIGN and bool(REFUSED.search(name))


def _proxy_without_credentials(value: str) -> bool:
    if "@" not in value:
        return True
    try:
        return urlsplit(value).username is None
    except ValueError:
        return False


def declared_env(step_env: Mapping[str, str], extra_names: Iterable[str]) -> Tuple[Dict[str, str], List[str]]:
    """The environment for the parts, and the ``extra_names`` that were refused."""
    rejected = sorted({name for name in extra_names if refused(name)})
    wanted = set(ALLOWED) | {name for name in extra_names if not refused(name)}
    env = {}
    for name, value in step_env.items():
        if refused(name):
            continue
        if name in PROXY_NAMES:
            if _proxy_without_credentials(value):
                env[name] = value
        elif name in wanted or name.startswith(ALLOWED_PREFIXES):
            env[name] = value
    return env, rejected
