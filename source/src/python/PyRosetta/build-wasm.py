#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# :noTabs=true:

# (c) Copyright Rosetta Commons Member Institutions.
# (c) This file is part of the Rosetta software suite and is made available under license.
# (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
# (c) For more information, see http://www.rosettacommons.org. Questions about this can be
# (c) addressed to University of Washington CoMotion, email: license@uw.edu.

"""
Build PyRosetta for WebAssembly (Pyodide).

Sibling to build.py. See ``.ai/adrs/0001-pyodide-integration.md`` for
the full design and ``.ai/specs/0008-cross-compile-pyrosetta-to-wasm.md``
for the cross-compile interface (Approach B).

Pipeline:

1. Phase 1 — install toolchain (uv, emsdk, pyodide-build) into
   ``source/build/PyRosetta-WASM/prefix/``.
2. Phase 2 — build: source ``emsdk_env.sh`` and invoke
   ``build.py --target wasm`` with Pyodide's cross-compile flags
   (``--cmake-toolchain``, ``--cflags``, ``--cxxflags``, ``--ldflags``,
   ``--python-include-dir``, ``--python-version``). build.py runs
   Binder + CMake configure + ninja end-to-end and emits a
   ``rosetta.so`` (wasm32-emscripten) under ``<build>/pyrosetta/``.
   Skipped by ``--skip-build-phase``.
3. Phase 3 — package: invoke ``pyodide build`` against the resulting
   ``setup.py``. The wheel post-processor renames the ``.so`` to
   carry the Pyodide ABI tag. Skipped by ``--skip-pyodide-build-phase``.
4. Phase 4 — test: install the wheel into a throwaway ``pyodide venv``
   and assert that ``import pyrosetta; pyrosetta.init()`` prints the
   M1 banner. Run only with ``--test``.

Host prerequisites:
    - git, curl, bash
    - cmake, ninja, and a host C/C++ compiler (required by the inner
      build.py / Binder build).
    - Python >= 3.8 to run this script (the build's target Python comes
      from uv).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Pinned toolchain versions (see ADR 0001 section 1).
# ---------------------------------------------------------------------------
PYODIDE_VERSION = "0.29.4"
PYTHON_MINOR = "3.13"  # Pyodide 0.29.4 ships CPython 3.13.2 internally.
EMSDK_VERSION = "4.0.9"
UV_VERSION = "0.11.14"

# `pyodide-build` lives in its own repo (https://github.com/pyodide/pyodide-build).
# The Pyodide release line and the standalone pyodide-build version line
# do not match — PyPI's pyodide-build versioning has drifted from the
# Pyodide release versioning since the repo split (2024). The canonical
# pyodide-build version for a given Pyodide release is the commit SHA
# referenced by the `pyodide-build` submodule in the main pyodide repo
# at the matching release tag. Bump this SHA together with PYODIDE_VERSION.
PYODIDE_BUILD_REPO = "https://github.com/pyodide/pyodide-build.git"
PYODIDE_BUILD_COMMIT = "720edf797d6ae22cb82c1159bd8d787b716ec863"  # submodule at pyodide tag 0.29.4

EMSDK_REPO = "https://github.com/emscripten-core/emsdk.git"
UV_RELEASE_URL = (
    "https://github.com/astral-sh/uv/releases/download/{ver}/"
    "uv-{triple}.tar.gz"
)


# ---------------------------------------------------------------------------
# Path layout.
# ---------------------------------------------------------------------------
def script_dir() -> Path:
    return Path(__file__).resolve().parent


def rosetta_source_path() -> Path:
    # source/src/python/PyRosetta/ -> source/
    return script_dir().parents[2]


def build_prefix_root() -> Path:
    """Where uv, emsdk, and the pyodide-build venv get installed."""
    return rosetta_source_path() / "build" / "PyRosetta-WASM" / "prefix"


def build_root(build_type: str) -> Path:
    """Where the WASM build actually happens (per-config)."""
    config = build_type.lower()
    return (
        rosetta_source_path()
        / "build"
        / "PyRosetta-WASM"
        / f"pyodide-{PYODIDE_VERSION}"
        / f"python-{PYTHON_MINOR}"
        / config
    )


def wheel_dist_dir(build_type: str) -> Path:
    """Where ``pyodide build`` drops wheels. Written by the package phase
    and read by the test phase, so both must agree on it."""
    return build_root(build_type) / "dist"


def pyodide_build_install_dir(prefix_root: Path) -> Path:
    return prefix_root / f"pyodide-build-{PYODIDE_VERSION}"


def xbuildenv_root_for(prefix_root: Path) -> Path:
    """Where ``pyodide xbuildenv install`` writes the cross-build env.
    Shared between the installer and the build phase so they agree."""
    return pyodide_build_install_dir(prefix_root) / "xbuildenv"


# ---------------------------------------------------------------------------
# Helpers (modernized from build.py).
# ---------------------------------------------------------------------------
def execute(
    message: str,
    *cmd: str,
    cwd: Path | None = None,
    env: dict | None = None,
) -> None:
    """Run a command, streaming output, abort on non-zero."""
    print(f"==> {message}")
    print(f"    $ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, cwd=str(cwd) if cwd else None, env=env)


def execute_shell(
    message: str,
    command: str,
    cwd: Path | None = None,
    env: dict | None = None,
) -> None:
    """Run a shell command. Use only when shell features are needed
    (env activation, here-docs, source)."""
    print(f"==> {message}")
    print(f"    $ {command}", flush=True)
    subprocess.run(
        ["bash", "-c", command], check=True, cwd=str(cwd) if cwd else None, env=env
    )


def signature_matches(signature_file: Path, signature: dict) -> bool:
    if not signature_file.is_file():
        return False
    try:
        return json.loads(signature_file.read_text()) == signature
    except json.JSONDecodeError:
        return False


def write_signature(signature_file: Path, signature: dict) -> None:
    signature_file.parent.mkdir(parents=True, exist_ok=True)
    signature_file.write_text(json.dumps(signature, sort_keys=True, indent=2))


def linux_triple() -> str:
    """Best-effort uv release-asset triple for the current host."""
    machine = platform.machine()
    if sys.platform != "linux":
        sys.exit(
            f"build-wasm.py currently supports Linux x86_64 hosts; "
            f"detected {sys.platform}. Add support and try again."
        )
    if machine == "x86_64":
        return "x86_64-unknown-linux-gnu"
    if machine in ("aarch64", "arm64"):
        return "aarch64-unknown-linux-gnu"
    sys.exit(f"Unsupported host architecture for uv: {machine}")


def download(url: str, dest: Path) -> None:
    print(f"==> Downloading {url}")
    print(f"    -> {dest}", flush=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Toolchain installs.
# ---------------------------------------------------------------------------
def install_uv(prefix_root: Path, version: str = UV_VERSION) -> Path:
    """Install uv into <prefix_root>/uv-<version>/. Return path to bin/uv."""
    triple = linux_triple()
    install_dir = prefix_root / f"uv-{version}"
    bin_dir = install_dir / "bin"
    uv_bin = bin_dir / "uv"
    signature_file = install_dir / ".signature.json"
    signature = {"tool": "uv", "version": version, "triple": triple}

    if signature_matches(signature_file, signature) and uv_bin.is_file():
        print(f"uv {version} already installed at {install_dir}")
        return uv_bin

    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True)
    bin_dir.mkdir()

    url = UV_RELEASE_URL.format(ver=version, triple=triple)
    sha_url = url + ".sha256"

    tarball = install_dir / "uv.tar.gz"
    sha_path = install_dir / "uv.tar.gz.sha256"

    download(url, tarball)
    download(sha_url, sha_path)

    expected_sha = sha_path.read_text().split()[0].strip()
    actual_sha = sha256_of(tarball)
    if expected_sha != actual_sha:
        sys.exit(
            f"SHA256 mismatch for uv tarball:\n"
            f"  expected: {expected_sha}\n"
            f"  actual:   {actual_sha}"
        )
    print(f"==> SHA256 verified: {actual_sha}")

    print(f"==> Extracting uv into {bin_dir}")
    with tarfile.open(tarball, "r:gz") as tf:
        # The tarball contains a top-level dir like "uv-<triple>/" with
        # uv and uvx inside. Extract those into bin/.
        for member in tf.getmembers():
            name = Path(member.name).name
            if name in ("uv", "uvx") and member.isfile():
                member.name = name
                tf.extract(member, path=bin_dir)
    if not uv_bin.is_file():
        sys.exit(f"uv binary not found at {uv_bin} after extraction")
    uv_bin.chmod(0o755)
    (bin_dir / "uvx").chmod(0o755)

    tarball.unlink()
    sha_path.unlink()

    write_signature(signature_file, signature)
    return uv_bin


def install_emsdk(prefix_root: Path, version: str = EMSDK_VERSION) -> Path:
    """Install Emscripten SDK at <prefix_root>/emsdk-<version>/. Return
    path to emsdk_env.sh (use with `bash -c 'source <path> && ...'`)."""
    install_dir = prefix_root / f"emsdk-{version}"
    signature_file = install_dir / ".signature.json"
    env_script = install_dir / "emsdk_env.sh"
    signature = {"tool": "emsdk", "version": version, "repo": EMSDK_REPO}

    if signature_matches(signature_file, signature) and env_script.is_file():
        print(f"emsdk {version} already installed at {install_dir}")
        return env_script

    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.parent.mkdir(parents=True, exist_ok=True)

    execute(
        f"Cloning emsdk into {install_dir}",
        "git",
        "clone",
        "--depth",
        "1",
        EMSDK_REPO,
        str(install_dir),
    )
    execute(
        f"Installing emsdk {version}",
        str(install_dir / "emsdk"),
        "install",
        version,
        cwd=install_dir,
    )
    execute(
        f"Activating emsdk {version}",
        str(install_dir / "emsdk"),
        "activate",
        version,
        cwd=install_dir,
    )

    if not env_script.is_file():
        sys.exit(f"emsdk_env.sh missing after install at {env_script}")

    write_signature(signature_file, signature)
    return env_script


def install_pyodide_build_env(
    prefix_root: Path,
    pyodide_version: str = PYODIDE_VERSION,
    python_minor: str = PYTHON_MINOR,
) -> Path:
    """Create a pyodide-build venv at
    <prefix_root>/pyodide-build-<version>/venv/. uv fetches CPython
    <python_minor> via python-build-standalone if not cached. Returns
    the path to the venv's bin/ directory.
    """
    uv_bin = install_uv(prefix_root)

    install_dir = pyodide_build_install_dir(prefix_root)
    venv_dir = install_dir / "venv"
    venv_bin = venv_dir / "bin"
    signature_file = install_dir / ".signature.json"
    src_dir = install_dir / "src"
    # Keep the xbuildenv inside install_dir so it is removed by the
    # existing shutil.rmtree on signature mismatch. Path is recorded
    # in the signature so moving the prefix triggers reinstall.
    xbuildenv_root = xbuildenv_root_for(prefix_root)
    xbuildenv_installed_marker = xbuildenv_root / pyodide_version / ".installed"
    signature = {
        "tool": "pyodide-build",
        "pyodide_version": pyodide_version,
        "python_minor": python_minor,
        "uv_version": UV_VERSION,
        "pyodide_build_commit": PYODIDE_BUILD_COMMIT,
        "xbuildenv_path": "xbuildenv",  # relative to install_dir
    }

    if (
        signature_matches(signature_file, signature)
        and (venv_bin / "pyodide").is_file()
        and xbuildenv_installed_marker.is_file()
    ):
        print(f"pyodide-build for Pyodide {pyodide_version} already installed at {install_dir}")
        return venv_bin

    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True)

    execute(
        f"Cloning pyodide-build into {src_dir}",
        "git",
        "clone",
        PYODIDE_BUILD_REPO,
        str(src_dir),
    )
    execute(
        f"Checking out pyodide-build at {PYODIDE_BUILD_COMMIT[:12]}",
        "git",
        "-C",
        str(src_dir),
        "checkout",
        "--detach",
        PYODIDE_BUILD_COMMIT,
    )

    execute(
        f"Creating venv with Python {python_minor} (uv-managed)",
        str(uv_bin),
        "venv",
        "--python",
        python_minor,
        str(venv_dir),
    )
    execute(
        f"Installing pyodide-build from {src_dir}",
        str(uv_bin),
        "pip",
        "install",
        "--python",
        str(venv_bin / "python"),
        str(src_dir),
    )
    execute(
        f"Installing Pyodide xbuildenv for {pyodide_version} into {xbuildenv_root}",
        str(venv_bin / "pyodide"),
        "xbuildenv",
        "install",
        "--path",
        str(xbuildenv_root),
        pyodide_version,
    )
    if not xbuildenv_installed_marker.is_file():
        sys.exit(
            f"xbuildenv install reported success but marker is missing: "
            f"{xbuildenv_installed_marker}"
        )

    write_signature(signature_file, signature)
    return venv_bin


# ---------------------------------------------------------------------------
# Build phase — shell out to build.py (Binder + CMake + ninja, end-to-end).
# ---------------------------------------------------------------------------
def discover_inner_build_root(build_type: str) -> Path:
    """Ask the inner build.py where it will write generation output.
    The returned path is the binding build root; ``setup.py`` lives at
    ``<root>/build/setup.py`` once the build phase has run."""
    result = subprocess.run(
        [sys.executable, "build.py", "--print-build-root", "--target", "wasm", "--type", build_type],
        cwd=str(script_dir()),
        check=True,
        capture_output=True,
        text=True,
    )
    # Inner build.py may emit informational lines (e.g. the WASM `NOTE:`)
    # before the build-root path. The path is always the final non-empty
    # stdout line.
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        sys.exit("build.py --print-build-root produced no output")
    return Path(lines[-1].strip())


def pyodide_config_dict(prefix_root: Path, pyodide_venv_bin: Path) -> dict[str, str]:
    """Parse ``pyodide config list`` into a dict. Values are stripped of
    their surrounding double quotes.

    Sets ``PYODIDE_XBUILDENV_PATH`` so pyodide resolves to the
    project-prefix xbuildenv (task 0004) rather than its default
    ``~/.cache/.pyodide-xbuildenv-*`` fallback."""
    pyodide_bin = pyodide_venv_bin / "pyodide"
    env = os.environ.copy()
    env["PYODIDE_XBUILDENV_PATH"] = str(xbuildenv_root_for(prefix_root))
    result = subprocess.run(
        [str(pyodide_bin), "config", "list"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    config: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        config[key.strip()] = value.strip().strip('"')
    return config


def ensure_libpython_stub(prefix_root: Path, py_minor: str) -> Path:
    """Pyodide's xbuildenv ships only Python headers, not a libpython
    archive — SIDE_MODULE wheels resolve Python symbols at import time
    via Pyodide's runtime, so no static link is needed. CMake's
    ``find_package(PythonLibs)`` insists on a file, though. Create an
    empty stub once under the prefix and hand it back; on Linux
    rosetta.cmake does not actually link against it (only Windows does)."""
    stub_dir = prefix_root / "wasm-stubs"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / f"libpython{py_minor}.so"
    if not stub.exists():
        stub.touch()
    return stub


def ensure_zlib_stub(prefix_root: Path, emsdk_env: Path) -> tuple[Path, Path]:
    """Provide a zlib include dir + library stub for CMake's
    ``find_package(ZLIB REQUIRED)`` call in rosetta.cmake. Emscripten's
    SIDE_MODULE wheels resolve zlib symbols against the Pyodide runtime
    at import time, so the library needs to contribute no symbols — but
    cmake still demands a real path and puts it on the link line, where
    wasm-ld rejects anything that is not a wasm file, an empty file
    included. So the stub is an object compiled from an empty
    translation unit rather than an empty file. Headers come from the
    host (``/usr/include/zlib.h`` + ``zconf.h``). Returns
    ``(include_dir, library_file)``."""
    stub_dir = prefix_root / "wasm-stubs" / "zlib"
    include_dir = stub_dir / "include"
    library_file = stub_dir / "libz.so"
    include_dir.mkdir(parents=True, exist_ok=True)
    for header in ("zlib.h", "zconf.h"):
        host_header = Path("/usr/include") / header
        target = include_dir / header
        if not target.exists():
            if not host_header.is_file():
                sys.exit(
                    f"Cannot find host zlib header {host_header}. Install "
                    f"`zlib1g-dev` (or equivalent) and re-run."
                )
            shutil.copy2(host_header, target)
    if not library_file.is_file() or library_file.stat().st_size == 0:
        empty_tu = stub_dir / "empty.c"
        empty_tu.touch()
        execute_shell(
            "Compiling zlib link stub",
            f"source {shlex.quote(str(emsdk_env))} >/dev/null && "
            f"emcc -c {shlex.quote(str(empty_tu))} -o {shlex.quote(str(library_file))}",
        )
    return include_dir, library_file


def run_build_phase(
    args: argparse.Namespace,
    prefix_root: Path,
    emsdk_env: Path,
    pyodide_venv_bin: Path,
) -> Path:
    """Run ``build.py --target wasm`` end-to-end (Binder + CMake configure
    + ninja) under a sourced emsdk env, with Pyodide's cross-compile flags
    forwarded via the new build.py options. Returns the inner binding
    build root (the directory whose ``build/`` contains ``setup.py``)."""
    inner_root = discover_inner_build_root(args.type)
    config = pyodide_config_dict(prefix_root, pyodide_venv_bin)
    required_keys = (
        "cmake_toolchain_file", "python_include_dir",
        "cflags", "cxxflags", "ldflags", "python_version",
    )
    missing = [k for k in required_keys if not config.get(k)]
    if missing:
        sys.exit(f"`pyodide config list` is missing required keys: {missing}")

    py_minor = ".".join(config["python_version"].split(".")[:2])
    python_lib_stub = ensure_libpython_stub(prefix_root, py_minor)
    zlib_include_dir, zlib_library = ensure_zlib_stub(prefix_root, emsdk_env)

    # Rosetta-WASM-specific C++ defines layered on top of Pyodide's cxxflags.
    # -DUNUSUAL_ALLOCATOR_DECLARATION: swap the `namespace std { template<typename>
    #  class allocator; }` forward-declaration in vector{0,1,L}.fwd.hh for a
    #  plain `#include <vector>`. The forward-decl form is ambiguous against
    #  emscripten's versioned-namespace libc++ (std::__2::allocator).
    rosetta_cxx_defines = "-DUNUSUAL_ALLOCATOR_DECLARATION"
    cxxflags = config["cxxflags"] + " " + rosetta_cxx_defines

    # Pyodide's ldflags end in -Oz, which drives emcc's post-link `wasm-opt`
    # pass. On a module this size that pass is both ruinous and disqualifying:
    #
    #  - it inlines every single-caller function without a size bound
    #    (binaryen's --one-caller-inline-max-function-size defaults to "all"),
    #    which fuses the per-translation-unit binding functions into one
    #    22 MB function -- past the 7,654,321-byte cap every wasm engine puts
    #    on a single function body, so the module will not even compile;
    #  - it hoists repeated values in __wasm_apply_data_relocs into locals that
    #    stay live across the whole body, which leaves wasm_split_functions.py
    #    nowhere safe to cut that function (it is over the cap on its own);
    #  - it took 13.75 h at ~39 GB RSS on the last full link.
    #
    # emcc takes the last -O it is given, so appending -O1 overrides it. That
    # keeps the linker's own output shape, which is splittable, at the cost of
    # the size reduction -Oz would have given.
    ldflags = config["ldflags"] + " -O1"

    inner_args = [
        "build.py",
        "--target", "wasm",
        "--cmake-toolchain", config["cmake_toolchain_file"],
        "--python-include-dir", config["python_include_dir"],
        "--python-lib", str(python_lib_stub),
        "--cflags", config["cflags"],
        "--cxxflags", cxxflags,
        "--ldflags", ldflags,
        "--python-version", py_minor,
        "--zlib-include-dir", str(zlib_include_dir),
        "--zlib-library", str(zlib_library),
        "--type", args.type,
        "-j", str(args.jobs),
    ]
    if args.version_file:
        inner_args += ["--version", args.version_file]

    quoted = " ".join(shlex.quote(a) for a in inner_args)
    shell_cmd = (
        f"set -e && "
        f"source {shlex.quote(str(emsdk_env))} >/dev/null && "
        f"{shlex.quote(sys.executable)} {quoted}"
    )
    execute_shell(
        "Running inner build.py (Binder + CMake + ninja) under emsdk env",
        shell_cmd,
        cwd=script_dir(),
    )
    return inner_root


# ---------------------------------------------------------------------------
# Build phase — invoke pyodide build against the generated setup.py.
# ---------------------------------------------------------------------------
def run_pyodide_build_phase(
    prefix_root: Path,
    pyodide_venv_bin: Path,
    emsdk_env: Path,
    args: argparse.Namespace,
    inner_build_root: Path,
) -> Path | None:
    """Invoke ``pyodide build`` on ``<inner_build_root>/build/``. Returns
    the wheel this run produced, or None if it did not produce exactly one.

    Identifying the wheel here, rather than globbing ``dist/`` later, is
    what stops a wheel left by an earlier run from standing in for one this
    run failed to make."""
    setup_dir = inner_build_root / "build"
    setup_py = setup_dir / "setup.py"
    if not setup_py.is_file():
        sys.exit(
            f"Expected setup.py at {setup_py} after the generation "
            f"phase, but it is missing. The inner build.py may have "
            f"changed; re-check the handoff path."
        )

    outdir = wheel_dist_dir(args.type)
    outdir.mkdir(parents=True, exist_ok=True)

    # Wheels already in the outdir, so the ones this run writes can be told
    # apart from leftovers. A rebuilt wheel keeps its name, hence the mtime.
    before = {w: w.stat().st_mtime for w in outdir.glob("*.whl")}

    xbuildenv = xbuildenv_root_for(prefix_root)
    pyodide_bin = pyodide_venv_bin / "pyodide"

    # Source emsdk_env.sh and set PYODIDE_XBUILDENV_PATH (task 0004) so
    # pyodide-build finds the prefix-local xbuildenv.
    shell_cmd = (
        f"set -e && "
        f"source {emsdk_env} >/dev/null && "
        f"export PYODIDE_XBUILDENV_PATH={xbuildenv} && "
        f"cd {setup_dir} && "
        f"{pyodide_bin} build --outdir {outdir}"
    )
    execute_shell("Running pyodide build", shell_cmd)

    produced = [
        w for w in sorted(outdir.glob("*.whl"))
        if before.get(w) != w.stat().st_mtime
    ]
    print()
    if len(produced) == 1:
        print(f"Wheel produced under {outdir}:")
        print(f"  {produced[0].name}")
        return produced[0]
    if not produced:
        print(f"WARNING: pyodide build exited 0 but wrote no wheel in {outdir}")
    else:
        print(f"WARNING: pyodide build wrote {len(produced)} wheels in {outdir}:")
        for w in produced:
            print(f"  {w.name}")
    return None


# ---------------------------------------------------------------------------
# Test phase — install the wheel into a Pyodide venv and run the smoke test.
# ---------------------------------------------------------------------------
# What `pyrosetta.init()` has to print for the M1 smoke test to pass. Only
# text that is stable from build to build: version strings, random seeds and
# database paths vary per run and are deliberately not asserted on. The two
# borders are the banner emitted by `pyrosetta/__init__.py:version()`.
SMOKE_TEST_REQUIRED_OUTPUT = (
    "┌" + "─" * 79 + "┐",
    "PyRosetta-4",
    "└" + "─" * 79 + "┘",
    "core.init:",
    "basic.random.init_random_generator:",
)

# Run from a file rather than `python -c`, and the difference is not
# cosmetic. `python -c` leaves `__main__` without a `__file__`, so
# `pyrosetta._is_interactive()` is True, `init()` defaults to
# `set_logging_handler="interactive"`, and `set_logging_sink()` calls
# `Tracer.super_mute(True)`: the banner then reaches stdout only through
# Python's `logging`, and the C++ tracer's own stdout — the path an
# emscripten stdout fault would break, and the one a browser console shows
# — is never exercised. From a file, `_is_interactive()` is False and
# nothing is muted.
SMOKE_TEST_SOURCE = "import pyrosetta\npyrosetta.init()\n"


def find_wheel_to_test(wheel_dir: Path) -> Path:
    """The single wheel in ``wheel_dir``, for the paths where the package
    phase did not just hand one over."""
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        sys.exit(
            f"No wheel to test in {wheel_dir}. Run without "
            f"--skip-pyodide-build-phase to build one."
        )
    if len(wheels) > 1:
        listed = "\n  ".join(w.name for w in wheels)
        sys.exit(
            f"Expected one wheel in {wheel_dir}, found {len(wheels)}:\n"
            f"  {listed}\n"
            f"Refusing to guess which one to test; remove the stale wheels."
        )
    return wheels[0]


def run_test_phase(
    prefix_root: Path,
    pyodide_venv_bin: Path,
    emsdk_env: Path,
    args: argparse.Namespace,
    wheel: Path,
) -> None:
    """Install the built wheel into a throwaway Pyodide venv and assert that
    ``import pyrosetta; pyrosetta.init()`` prints the M1 banner.

    The venv is rebuilt from scratch on every run, at the cost of
    reinstalling a large wheel. Reusing one would not re-install: given a
    wheel whose version is already present, pip skips it and still exits 0
    ("pyrosetta is already installed with the same version as the provided
    wheel"), so the test would silently pass against the previous build's
    extension module.

    Installing the wheel is also what keeps the test honest. The other
    obvious route — mounting the inner build tree into the Pyodide
    filesystem — silently tests nothing: its ``pyrosetta/__init__.py`` is a
    relative symlink whose target is outside the mount root, so the mounted
    ``pyrosetta`` has no ``__init__.py`` at all, imports as an empty
    namespace package, and passes.
    """
    venv_dir = build_root(args.type) / "test-venv"
    if venv_dir.exists():
        shutil.rmtree(venv_dir)

    preamble = (
        # emsdk_env.sh announces itself on stderr, which would otherwise land
        # in the captured smoke-test output. EMSDK_QUIET is emsdk's own switch
        # for that, and unlike a blanket 2>/dev/null it keeps real errors.
        f"set -e && export EMSDK_QUIET=1 && "
        f"source {shlex.quote(str(emsdk_env))} >/dev/null && "
        # The Pyodide venv's `python` launcher picks its wasm runtime with
        # `which node`, and emsdk_env.sh exports EMSDK_NODE without putting
        # it on PATH. Without this line the test runs on whatever node the
        # host happens to have — or fails outright on a host with none —
        # instead of the node emsdk installed for us.
        'export PATH="$(dirname "${EMSDK_NODE:?emsdk_env.sh did not export '
        'EMSDK_NODE}")":$PATH && '
        f"export PYODIDE_XBUILDENV_PATH="
        f"{shlex.quote(str(xbuildenv_root_for(prefix_root)))} && "
        # `pyodide venv` writes launchers that shell back out to the `pyodide`
        # CLI by name, so the CLI has to be on PATH; calling it by absolute
        # path below is not enough on its own.
        f"export PATH={shlex.quote(str(pyodide_venv_bin))}:$PATH && "
    )

    execute_shell(
        f"Creating Pyodide venv at {venv_dir} and installing {wheel.name}",
        preamble
        + f"pyodide venv {shlex.quote(str(venv_dir))} && "
        + f"{shlex.quote(str(venv_dir / 'bin' / 'pip'))} install "
        + shlex.quote(str(wheel)),
    )

    smoke_script = build_root(args.type) / "smoke-test.py"
    smoke_script.write_text(SMOKE_TEST_SOURCE, encoding="utf-8")

    command = preamble + (
        f"{shlex.quote(str(venv_dir / 'bin' / 'python'))} "
        f"{shlex.quote(str(smoke_script))}"
    )
    print(f"==> Running the headless smoke test from {smoke_script}")
    print(f"    $ {command}", flush=True)
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout + result.stderr
    print(output)

    if result.returncode != 0:
        sys.exit(
            f"Smoke test FAILED: the command above exited "
            f"{result.returncode}; its output has the cause."
        )

    missing = [s for s in SMOKE_TEST_REQUIRED_OUTPUT if s not in output]
    if missing:
        listed = "\n  ".join(repr(s) for s in missing)
        sys.exit(
            f"Smoke test FAILED: {smoke_script.name} exited 0, but "
            f"{len(missing)} of the {len(SMOKE_TEST_REQUIRED_OUTPUT)} "
            f"required substrings are absent from its output:\n  {listed}"
        )

    print(f"Smoke test PASSED: {wheel.name} initialises PyRosetta under Pyodide.")


# ---------------------------------------------------------------------------
# CLI / main.
# ---------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build PyRosetta for WebAssembly (Pyodide). Sibling of build.py. "
            "Phase 1: auto-install the WASM toolchain (uv, emsdk, pyodide-build) "
            "under source/build/PyRosetta-WASM/prefix/. "
            "Phase 2: source emsdk_env.sh and invoke build.py --target wasm "
            "(Binder + CMake configure + ninja, all in one). "
            "Phase 3: invoke `pyodide build` against the resulting setup.py to "
            "package a wheel with the cross-compiled extension module. "
            "Phase 4 (--test): install that wheel into a throwaway Pyodide venv "
            "and assert that `import pyrosetta; pyrosetta.init()` prints the "
            "M1 banner."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=1,
        help="Parallel jobs for downstream tools (default 1).",
    )
    parser.add_argument(
        "--type", default="Release",
        choices=["Release", "Debug", "MinSizeRel", "RelWithDebInfo"],
        help="Build type. Default Release.",
    )
    parser.add_argument(
        "--skip-build-phase", action="store_true",
        help="Skip Phase 2 (inner build.py: Binder + CMake + ninja). "
             "Useful when iterating on Phase 3 against an already-built "
             "tree.",
    )
    parser.add_argument(
        "--skip-pyodide-build-phase", action="store_true",
        help="Skip Phase 3 (pyodide build). Without --test the script stops "
             "after Phase 2 (or after the toolchain install if "
             "--skip-build-phase is also set); with --test it goes on to test "
             "whichever wheel is already in the build root's dist/.",
    )
    parser.add_argument(
        "--print-build-root", action="store_true",
        help="Print the WASM build root path and exit.",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Phase 4: run the headless smoke test. Installs the wheel into "
             "a throwaway Pyodide venv under the build root and asserts that "
             "`import pyrosetta; pyrosetta.init()` prints the M1 banner. "
             "Pass both --skip flags to test an already-built wheel.",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Remove the WASM build dir for this --type. "
             "Does not touch the native build dir or the toolchain prefix.",
    )
    parser.add_argument(
        "--version-file", default=None,
        help="JSON version file (pass-through to inner build.py --version).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.print_build_root:
        print(build_root(args.type))
        return 0

    if args.clean:
        target = build_root(args.type)
        if target.exists():
            print(f"Removing {target}")
            shutil.rmtree(target)
        else:
            print(f"Nothing to clean at {target}")
        return 0

    prefix = build_prefix_root()
    prefix.mkdir(parents=True, exist_ok=True)

    phases = []
    phases.append("toolchain install")
    if not args.skip_build_phase:
        phases.append("build (build.py --target wasm: Binder + CMake + ninja)")
    if not args.skip_pyodide_build_phase:
        phases.append("package (pyodide build)")
    if args.test:
        phases.append("test (headless smoke test)")
    print(f"PyRosetta-WASM toolchain prefix: {prefix}")
    print(f"PyRosetta-WASM build root:       {build_root(args.type)}")
    print(f"Phases to run:                   {', '.join(phases)}")
    print()

    # Phase 1.
    uv_bin = install_uv(prefix)
    emsdk_env = install_emsdk(prefix)
    pyodide_venv_bin = install_pyodide_build_env(prefix)

    print()
    print("Toolchain ready.")
    print(f"  uv binary:           {uv_bin}")
    print(f"  emsdk env script:    {emsdk_env}")
    print(f"  pyodide-build venv:  {pyodide_venv_bin}")
    print()

    # Phase 2.
    if args.skip_build_phase:
        print("Skipping build phase (--skip-build-phase).")
        inner_build_root = None
    else:
        inner_build_root = run_build_phase(args, prefix, emsdk_env, pyodide_venv_bin)

    # Phase 3.
    if args.skip_pyodide_build_phase:
        print("Skipping pyodide build phase (--skip-pyodide-build-phase).")
        wheel = None
    else:
        if inner_build_root is None:
            # Phase 2 was skipped, so ask the inner build.py where it left
            # the generated setup.py. Only the package phase needs this, and
            # it shells out to build.py, so it stays out of the paths that
            # do not.
            inner_build_root = discover_inner_build_root(args.type)
        wheel = run_pyodide_build_phase(
            prefix, pyodide_venv_bin, emsdk_env, args, inner_build_root
        )
        if args.test and wheel is None:
            sys.exit(
                "The package phase produced no wheel to smoke-test. Falling "
                "back to whatever dist/ still holds would report a pass for "
                "a build that made nothing."
            )

    # Phase 4.
    if args.test:
        if wheel is None:
            wheel = find_wheel_to_test(wheel_dist_dir(args.type))
        run_test_phase(prefix, pyodide_venv_bin, emsdk_env, args, wheel)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
