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
   carry the Pyodide ABI tag. The wheel is then rewritten without the
   database subtrees outside core protein modelling, which takes it
   from about 964 MB to about 172 MB; ``--database full`` keeps it
   whole. Skipped by ``--skip-pyodide-build-phase``.
4. Phase 4 — test: install the wheel into a throwaway ``pyodide venv``
   and assert that ``import pyrosetta; pyrosetta.init()`` prints the
   M1 banner. Run only with ``--test``.
5. Phase 5 — browser test: serve the wheel and the Pyodide runtime on
   loopback, load them in a pinned ``chrome-headless-shell``, and assert
   the same banner against what PyRosetta prints inside the browser.
   Run only with ``--browser-test``.

Host prerequisites:
    - git, curl, bash
    - cmake, ninja, and a host C/C++ compiler (required by the inner
      build.py / Binder build).
    - Python >= 3.8 to run this script (the build's target Python comes
      from uv).
    - For ``--browser-test`` only: the shared libraries Chrome links
      against. The phase names any that are missing.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import os
import platform
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

# The browser the --browser-test phase drives. Chrome for Testing is Chrome
# built for automation and pinned by version, which is what makes a browser
# result reproducible; `chrome-headless-shell` is its headless-only build, a
# third of the size of full Chrome. Unlike the rest of the toolchain it links
# against the host's own libraries — see missing_chrome_libraries.
#
# Google publishes no checksum next to the archive, so the expected digest is
# recorded here and verified on download, the way install_uv verifies the
# .sha256 that uv does publish. Bump the two together; find the current version
# in
# https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json
CHROME_VERSION = "153.0.8010.52"
CHROME_SHA256 = "944dc1eae654637fed4d57650198774f9c43b45f34e48febb84f43c541b5de76"
CHROME_RELEASE_URL = (
    "https://storage.googleapis.com/chrome-for-testing-public/{ver}/"
    "linux64/chrome-headless-shell-linux64.zip"
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


def pyodide_browser_dist_dir(prefix_root: Path) -> Path:
    """The browser build of the Pyodide runtime — ``pyodide.js``,
    ``pyodide.asm.wasm``, ``python_stdlib.zip``, ``pyodide-lock.json`` — which
    ships inside the cross-build environment. The browser test serves these,
    so it needs no second copy of Pyodide from the network."""
    return (
        xbuildenv_root_for(prefix_root)
        / PYODIDE_VERSION
        / "xbuildenv"
        / "pyodide-root"
        / "dist"
    )


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


def emsdk_wasm_opt(emsdk_env: Path) -> Path:
    """Path to binaryen's ``wasm-opt`` inside an installed emsdk.

    The post-link size pass runs from CMake, which has no sourced emsdk env
    and so cannot find it on PATH; pass the absolute path instead."""
    wasm_opt = emsdk_env.parent / "upstream" / "bin" / "wasm-opt"
    if not wasm_opt.is_file():
        sys.exit(f"wasm-opt is missing from the emsdk install at {wasm_opt}")
    return wasm_opt


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


def install_chrome_headless_shell(
    prefix_root: Path,
    version: str = CHROME_VERSION,
    expected_sha256: str = CHROME_SHA256,
) -> Path:
    """Install chrome-headless-shell into
    <prefix_root>/chrome-headless-shell-<version>/. Return the browser binary.

    ``version`` and ``expected_sha256`` name one release between them, so they
    move together: Google publishes no checksum next to the archive, leaving
    nothing to derive the second from the first.

    Only the --browser-test phase calls this, so an ordinary build never pays
    the download."""
    install_dir = prefix_root / f"chrome-headless-shell-{version}"
    chrome_bin = (
        install_dir / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    )
    signature_file = install_dir / ".signature.json"
    signature = {
        "tool": "chrome-headless-shell",
        "version": version,
        "sha256": expected_sha256,
    }

    if signature_matches(signature_file, signature) and chrome_bin.is_file():
        print(f"chrome-headless-shell {version} already installed at {install_dir}")
        return chrome_bin

    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True)

    archive = install_dir / "chrome-headless-shell.zip"
    download(CHROME_RELEASE_URL.format(ver=version), archive)

    actual_sha = sha256_of(archive)
    if actual_sha != expected_sha256:
        sys.exit(
            f"SHA256 mismatch for the chrome-headless-shell archive:\n"
            f"  expected: {expected_sha256}\n"
            f"  actual:   {actual_sha}"
        )
    print(f"==> SHA256 verified: {actual_sha}")

    print(f"==> Extracting chrome-headless-shell into {install_dir}")
    with zipfile.ZipFile(archive) as archive_file:
        archive_file.extractall(install_dir)
    if not chrome_bin.is_file():
        sys.exit(f"chrome-headless-shell not found at {chrome_bin} after extraction")
    # ZipFile.extractall drops the executable bit.
    chrome_bin.chmod(0o755)

    archive.unlink()
    write_signature(signature_file, signature)
    return chrome_bin


def missing_chrome_libraries(chrome_bin: Path) -> list[str]:
    """Shared libraries the browser needs that this host does not have.

    The browser is the one piece of the toolchain that cannot be made
    self-contained by downloading it: Chrome links against the distribution's
    own libraries. Naming exactly which are absent turns an opaque startup
    failure into an apt-get line.

    An empty list also means "could not tell" on a host with no ``ldd``; the
    browser then reports the problem itself."""
    try:
        result = subprocess.run(
            ["ldd", str(chrome_bin)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return []
    return sorted(
        {
            line.split("=>")[0].strip()
            for line in result.stdout.splitlines()
            if "not found" in line
        }
    )


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
    #  - it took 13.75 h at ~39 GB RSS on the last full link, almost all of it
    #    spent optimising the fused body the first bullet describes.
    #
    # emcc takes the last -O it is given, so appending -O1 overrides it. That keeps
    # the linker's own output shape, which is splittable. The size is not given up:
    # rosetta.cmake runs wasm-opt after the split instead.
    #
    # Bounding the inlining is not on its own enough to make the link-time pass
    # usable, even though -sBINARYEN_EXTRA_PASSES would carry the option through
    # (emscripten forwards a '-'-prefixed entry verbatim). The ordering is what
    # rules it out: emcc's pass runs during the link, so it would see
    # __wasm_apply_data_relocs whole and over the cap, and leave it unsplittable.
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
        "--wasm-opt", str(emsdk_wasm_opt(emsdk_env)),
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
# Database subsetting — drop the parts of the Rosetta database a core
# protein-modelling wheel does not need.
# ---------------------------------------------------------------------------
# The database is 95% of the wheel — 912.6 MB of 964.1 MB compressed — and
# `pyrosetta.init()` reads none of it: traced through the filesystem, init
# opens all 540 directories and not one of the 7,623 files. A run that then
# builds a pose, scores it with ref2015, repacks and minimises reads 896 of
# them, 21.3 MB compressed.
#
# So the default wheel carries a subset. A subtree is dropped when it serves a
# protocol family outside core protein modelling, or when it is a legacy or
# alternative parameterisation that only a non-default flag selects.
#
# Two things override that, and both cost far less than they look:
#
#   - Anything whose absence is a hard exit or a silently wrong answer, rather
#     than a clean failure, stays regardless of which subtree it sits in.
#     `sampling/` is 264 MB of fragment libraries wrapped around 1.1 MB that
#     core modelling needs: `SASA-masks.dat` and `SASA-angles.dat`, which
#     `core/scoring/sasa.cc:110,130` streams into fixed arrays without ever
#     checking `good()` — absent, every SASA number is quietly computed against
#     a zero-filled table — and `relax_scripts/`, without which
#     `RelaxScriptManager.cc:168` exits and FastRelax cannot run. Likewise
#     `chemical/pdb_components/` is 95 MB of ligand dictionary around a 93-byte
#     `override.txt` that `GlobalResidueTypeSet.cc:901` exits without, on the
#     first PDB residue it does not recognise.
#
#   - `-beta` / `-beta_nov16` is a mainstream score function, not an exotic
#     correction, and `score_function_corrections.cc:1971` points `dun10_dir`
#     at `rotamer/beta_nov2016` when it is passed, so that stays too.
#
# The rule is deliberately coarser than the 896 files a traced run actually
# reads, and costs about 130 MB more than they would. A list derived from one
# trace would leave the first protocol that stepped outside it failing at
# runtime on a missing file, with nothing to suggest the wheel was the reason.
# `--database full` ships the database whole.
DATABASE_ROOT_IN_WHEEL = "pyrosetta/database/"

CORE_DATABASE_DROPPED_SUBTREES = (
    # Protocol families outside core protein modelling.
    "chemical/pdb_components/components.",  # the PDB ligand dictionary, 41 .cif
    "chemical/rdkit/",
    "external/",                     # SVM models, SPARTA+
    "protocol_data/",                # antibody, splice, protein_mpnn, tensorflow
    "rotamer/ncaa_rotlibs/",         # non-canonical amino acids
    "rotamer/peptoid_rotlibs/",
    "sampling/antibodies/",
    "sampling/disulfide_jump_database_wip.dat",
    "sampling/filtered.vall.",       # fragment libraries: the four vall files
    "sampling/fragpicker_rama_tables/",
    "sampling/orientations/",
    "sampling/rna/",
    "sampling/small.vall.gz",
    "sampling/spheres/",
    "sampling/ss_fragfiles/",
    "sampling/vall.",
    "scoring/loop_close/",           # KIC loop-closure statistics
    "scoring/qsar/",
    "scoring/rna/",
    "sequence/genome_9mers/",
    "sequence/mhc_",                 # mhc_pssms, mhc_rank_svm_scores, mhc_svms
    "sequence/tcell_ep_9mers/",
    # Legacy or alternative parameterisations, each behind a non-default flag.
    "rotamer/ExtendedOpt1-5/",       # dun10_dir's declared default, shadowed
                                     # at runtime by the shapovalov fixes
    "rotamer/bbdep02.May.sortlib",   # the 2002 Dunbrack library, both copies
    "rotamer/cenrot_dunbrack.lib",   # centroid rotamers, -score:cenrot
    "rotamer/corrections_conway2016/",
)

# rotamer/shapovalov/ carries the 2010 Dunbrack library at six smoothing
# levels of about 20 MB each, alike in coverage and differing in how far the
# probabilities are smoothed. -shap_dun10_dir selects one and
# score_function_corrections.cc:620 defaults it to this one, which is also the
# only level a traced default-flags run reads, so the other five go. Handled
# apart from the list above because it keeps a subtree rather than dropping one.
CORE_DATABASE_KEPT_SHAPOVALOV = "rotamer/shapovalov/StpDwn_0-0-0/"

# Read and written a megabyte at a time, so repacking a 964 MB wheel does not
# hold a 282 MB rosetta.so in memory to copy it.
WHEEL_COPY_CHUNK_BYTES = 1024 * 1024


def core_database_keeps(relative_path: str) -> bool:
    """Whether the core wheel keeps this database file.

    The path is relative to the database root inside the wheel, so
    "scoring/score_functions/rama/fd/all.ramaProb", not the "pyrosetta/
    database/" prefix that precedes it."""
    if relative_path.startswith("rotamer/shapovalov/"):
        return relative_path.startswith(CORE_DATABASE_KEPT_SHAPOVALOV)
    return not relative_path.startswith(CORE_DATABASE_DROPPED_SUBTREES)


def copy_wheel_entry(
    source: zipfile.ZipFile, target: zipfile.ZipFile, entry: zipfile.ZipInfo
) -> tuple[str, str, int]:
    """Copy one entry between wheels and return its RECORD row.

    Compression method, timestamp and mode are carried across so that only
    the dropped files distinguish the rewritten wheel from its source."""
    copy = zipfile.ZipInfo(entry.filename, date_time=entry.date_time)
    copy.compress_type = entry.compress_type
    copy.external_attr = entry.external_attr
    digest = hashlib.sha256()
    size = 0
    # Writing through ZipFile.open refuses anything past 2 GB unless ZIP64 is
    # asked for up front, because it has to size the header before it has the
    # data. No entry is near that today — rosetta.so is the largest at 282 MB
    # — so this only keeps a future one from failing mid-repack.
    zip64 = entry.file_size >= 2**31
    with source.open(entry) as reader, target.open(copy, "w", force_zip64=zip64) as writer:
        while chunk := reader.read(WHEEL_COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            writer.write(chunk)
    encoded = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
    return entry.filename, f"sha256={encoded}", size


def write_core_database_wheel(wheel: Path) -> Path:
    """Rewrite ``wheel`` without the database files the core policy drops.

    The wheel is replaced rather than written alongside, because the test
    phases take the single wheel in dist/ and would otherwise have two to
    choose between — and the one they picked would decide what the smoke
    test actually proved."""
    with zipfile.ZipFile(wheel) as source:
        record_name = next(
            (n for n in source.namelist() if n.endswith(".dist-info/RECORD")), None
        )
        if record_name is None:
            sys.exit(
                f"{wheel} carries no .dist-info/RECORD, so it is not a wheel "
                f"this can safely rewrite. Refusing to repack it."
            )

        temporary = wheel.with_name(wheel.name + ".repacking")
        rows: list[tuple[str, str, int]] = []
        kept = dropped = 0
        dropped_bytes = 0
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
                for entry in source.infolist():
                    name = entry.filename
                    if name == record_name:
                        continue  # rewritten below, once the kept set is known
                    if name.startswith(DATABASE_ROOT_IN_WHEEL):
                        relative = name[len(DATABASE_ROOT_IN_WHEEL):]
                        if not core_database_keeps(relative):
                            dropped += 1
                            dropped_bytes += entry.compress_size
                            continue
                        kept += 1
                    row = copy_wheel_entry(source, target, entry)
                    if not name.endswith("/"):
                        rows.append(row)

                # RECORD is a CSV file and four database paths really do
                # contain a comma, so it is written with the csv module
                # rather than by joining on one. Its own line carries no
                # hash or size, which is what PEP 376 asks for.
                record = io.StringIO()
                record_writer = csv.writer(record, lineterminator="\n")
                record_writer.writerows(rows)
                record_writer.writerow((record_name, "", ""))
                target.writestr(record_name, record.getvalue())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    before = wheel.stat().st_size
    os.replace(temporary, wheel)
    after = wheel.stat().st_size
    print()
    print(f"Database subset applied to {wheel.name}:")
    print(f"  database files kept:    {kept:,}")
    print(f"  database files dropped: {dropped:,} ({dropped_bytes:,} compressed bytes)")
    print(f"  wheel: {before:,} -> {after:,} bytes")
    return wheel


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
# Browser test phase — serve the wheel to a real browser and assert the banner.
# ---------------------------------------------------------------------------
# Measured at 42 s end to end on the development host: 2 s to boot Pyodide,
# 9 s to stream the wheel in, 23 s to install it, 7 s to initialise PyRosetta.
# The ceiling is far above that because a cold CDN fetch and a slower disk both
# have to fit under it, while a genuine hang still ends the run.
BROWSER_TEST_TIMEOUT_SECONDS = 900

# How often the browser is asked whether it has a verdict yet. Every poll also
# samples its memory, so this is the sampling period too. Reading smaps_rollup
# walks the target's page tables under its mmap lock, which a renderer
# allocating gigabytes is itself contending for, so a sample costs more the
# more the browser holds: measured at roughly 20 ms per GB, 8 ms with the
# browser idle and 122 ms at a 6 GB peak. Polling and sampling in lockstep
# bounds that: the loop turns over in this interval plus one sample, so the
# gap between samples grows by the cost of a sample rather than in proportion
# to it — 1.12 s at the 6 GB peak measured. A schedule that stretched as the
# browser grew would instead resolve a large configuration worst, which is the
# bias this phase exists to avoid.
BROWSER_POLL_INTERVAL_SECONDS = 1.0

# Packages the cross-build environment does not ship — numpy, which PyRosetta
# requires — come from the CDN Pyodide itself would use, the same one the
# headless test installs numpy from.
PYODIDE_PACKAGE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full"

# The server hands out a fixed set of files from two directories. Request paths
# are matched against this before they are used to name anything, and a leading
# dot is rejected separately so that "..", which is all legal characters, can
# not walk out of either directory.
SERVABLE_FILE_NAME = re.compile(r"[A-Za-z0-9._+-]+")

# A request line arrives from the network, so escape its control characters
# before printing one. BaseHTTPRequestHandler.log_message does this for the
# same reason, and this handler overrides it.
CONTROL_CHARACTER_ESCAPES = {
    code: f"\\x{code:02x}" for code in [*range(0x20), 0x7F]
}


def servable_name(name: str) -> bool:
    return bool(SERVABLE_FILE_NAME.fullmatch(name)) and not name.startswith(".")


def content_type_for(name: str) -> str:
    """Content types for the few suffixes the Pyodide runtime is served with.
    ``application/wasm`` is the one that has to be right: a browser refuses to
    stream-compile a module served as anything else."""
    if name.endswith((".js", ".mjs")):
        return "text/javascript"
    if name.endswith(".wasm"):
        return "application/wasm"
    if name.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


def redact_token(text: str) -> str:
    """Blank the value of a ``token=`` query parameter in text about to be
    printed. The token is what stops anything else on the host from posting a
    verdict for this run, so it does not belong in a build log."""
    return re.sub(r"(token=)[^\s&\"]+", r"\1<redacted>", text)


def proportional_memory(process: Path, resident_bytes: int) -> int:
    """One process's share of the memory it has resident, in bytes.

    ``Pss`` divides every shared page among the processes mapping it. Falls
    back to the resident set on a kernel that does not report ``Pss``, which
    then over-counts rather than reporting nothing."""
    try:
        for line in (process / "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        pass
    return resident_bytes


def process_tree_memory(pid: int) -> tuple[int, int]:
    """Memory held by a process and its descendants: (proportional, resident).

    What a browser costs to run this wheel is the question behind the whole
    phase, and Chrome spreads that cost across a browser process and a
    renderer child that share large read-only mappings — the 197 MB binary,
    the ICU tables, the zygote's copy-on-write heap. Summing each process's
    resident set counts those pages once per process, so the proportional
    total is the honest figure and the resident sum its upper bound."""
    parent_of: dict[int, int] = {}
    resident: dict[int, int] = {}
    page_size = os.sysconf("SC_PAGE_SIZE")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm sits in parentheses and may itself contain spaces, so the
            # fields after it are found from the last ") " rather than by split.
            after_comm = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            parent_of[int(entry.name)] = int(after_comm[1])
            resident[int(entry.name)] = (
                int((entry / "statm").read_text().split()[1]) * page_size
            )
        except (OSError, IndexError, ValueError):
            continue  # The process ended between listing /proc and reading it.

    children: dict[int, list[int]] = {}
    for child, parent in parent_of.items():
        children.setdefault(parent, []).append(child)

    # /proc is read one process at a time, so a pid reused mid-scan can leave
    # two entries recorded as each other's parent. Without `seen` that cycle
    # would spin here forever, inside the loop that is supposed to be timing
    # the browser out.
    seen: set[int] = set()
    proportional_total = 0
    resident_total = 0
    pending = [pid]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        resident_bytes = resident.get(current, 0)
        resident_total += resident_bytes
        proportional_total += proportional_memory(
            Path("/proc") / str(current), resident_bytes
        )
        pending.extend(children.get(current, []))
    return proportional_total, resident_total


class MemorySampler:
    """Peak memory of the browser process tree, sampled while the page works.

    Where the maximum falls depends on the wheel, so nothing here assumes.
    Against the 188 MB core wheel memory climbs until the page posts its
    verdict and the run stops right there — measured, the final second still
    added 0.4 GB, which is how the same work reported 4.3 GB on the 5 s
    schedule this phase used to keep and 6.0 GB on a 1 s one. Against a large
    wheel the maximum is interior instead: ``wasm_browser_test.html`` unlinks
    the archive as soon as micropip has unpacked it, handing a ~1 GB wheel's
    tab about that much back before PyRosetta starts, and 0031 attributed the
    9.8 GB peak of the 966 MB wheel to that unpack. So the peak is taken from
    a sample on every poll, plus one more when the verdict arrives.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.peak_proportional = 0
        self.peak_resident = 0
        self.samples = 0
        self.cost = 0.0

    def sample(self) -> None:
        """Record one reading of the tree's memory, and what taking it cost.

        The cost is worth keeping because it grows with the memory being
        measured, so the phase can report how much of the run it spent looking
        rather than leave the perturbation to be guessed at."""
        started = time.monotonic()
        proportional, resident = process_tree_memory(self.pid)
        self.cost += time.monotonic() - started
        self.peak_proportional = max(self.peak_proportional, proportional)
        self.peak_resident = max(self.peak_resident, resident)
        self.samples += 1


def write_private_file(path: Path, text: str) -> None:
    """Write a file only its owner can read.

    ``Path.write_text`` would leave it at the umask default, which on most
    hosts means world-readable."""
    path.unlink(missing_ok=True)  # O_CREAT leaves an existing file's mode.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def chrome_log_tail(chrome_log: Path, lines: int = 25) -> str:
    """The end of the browser's own output, for a failure message.

    A renderer killed for running out of memory — the failure this phase
    exists to catch — announces itself here and nowhere else."""
    try:
        recorded = chrome_log.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return "(the browser wrote nothing)"
    return "\n".join(recorded[-lines:]) or "(the browser wrote nothing)"


class BrowserTestServer(ThreadingHTTPServer):
    """Serves the test page, the Pyodide runtime and the wheel on loopback.

    Threaded because the page posts progress on a second connection while the
    ~1 GB wheel is still streaming on the first."""

    daemon_threads = True

    def __init__(
        self,
        page_html: bytes,
        pyodide_dist: Path,
        wheel: Path,
        token: str,
        results: queue.Queue,
    ) -> None:
        # Port 0 lets the OS pick a free port, so concurrent runs do not
        # collide; loopback keeps the wheel off the network.
        super().__init__(("127.0.0.1", 0), BrowserTestHandler)
        self.page_html = page_html
        self.pyodide_dist = pyodide_dist
        self.wheel = wheel
        self.token = token
        self.results = results


class BrowserTestHandler(BaseHTTPRequestHandler):
    """The harness end of the conversation with the page.

    The page reports its own progress and verdict by POST instead of the
    harness reading them out of the browser. That is what keeps this phase
    dependency-free: driving the browser over the DevTools protocol would mean
    a WebSocket client, and there is none in the standard library."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *fmt_args) -> None:
        # The page's URL carries the result token, and a request line lands in
        # build and CI logs. Redacting keeps the token to the run it belongs to.
        line = redact_token(fmt % fmt_args).translate(CONTROL_CHARACTER_ESCAPES)
        print(f"    [server] {line}", flush=True)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_bytes(self.server.page_html, "text/html; charset=utf-8")
        elif path.startswith("/pyodide/"):
            self.serve_pyodide_file(path[len("/pyodide/") :])
        elif path.startswith("/wheel/"):
            self.serve_wheel(path[len("/wheel/") :])
        else:
            self.send_bytes(b"not found\n", "text/plain", status=404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_bytes(b"malformed JSON\n", "text/plain", status=400)
            return

        # The token is what makes a report *this* run's report: without it a
        # browser left over from an earlier run, or any other process on the
        # host, could answer for the page just launched.
        token = payload.get("token", "") if isinstance(payload, dict) else ""
        if not secrets.compare_digest(str(token), self.server.token):
            self.send_bytes(b"bad token\n", "text/plain", status=403)
            return

        if path == "/log":
            print(f"    [page] {payload.get('message', '')}", flush=True)
        elif path == "/result":
            self.server.results.put(payload)
        else:
            self.send_bytes(b"not found\n", "text/plain", status=404)
            return
        self.send_bytes(b"", "text/plain")

    def serve_pyodide_file(self, name: str) -> None:
        if not servable_name(name):
            self.send_bytes(b"bad name\n", "text/plain", status=400)
            return
        local = self.server.pyodide_dist / name
        if local.is_file():
            self.send_file(local, content_type_for(name))
        else:
            self.redirect(f"{PYODIDE_PACKAGE_CDN}/{name}")

    def serve_wheel(self, name: str) -> None:
        if name != self.server.wheel.name:
            self.send_bytes(b"not found\n", "text/plain", status=404)
            return
        self.send_file(self.server.wheel, "application/octet-stream")

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        try:
            with open(path, "rb") as source:
                shutil.copyfileobj(source, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            # The browser went away mid-transfer: a timeout or a failure that
            # the phase itself reports. Nothing useful to add here.
            self.close_connection = True

    def redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()


def wait_for_browser_result(
    browser: subprocess.Popen,
    results: queue.Queue,
    sampler: MemorySampler,
    started: float,
    chrome_log: Path,
) -> dict:
    """Wait for the page to post its verdict, sampling memory as it works.

    The poll period doubles as the sampling period — see
    ``BROWSER_POLL_INTERVAL_SECONDS``. One more sample is taken once the
    verdict is in hand: it arrives at the instant PyRosetta finishes
    initialising, which no poll lands on, and against the core wheel that is
    the largest the browser ever gets.

    Exits rather than returning if the browser dies or the run overruns, since
    neither leaves a verdict to report."""
    while True:
        try:
            report = results.get(timeout=BROWSER_POLL_INTERVAL_SECONDS)
            break
        except queue.Empty:
            pass
        sampler.sample()
        waited = time.monotonic() - started
        if browser.poll() is not None:
            sys.exit(
                f"Browser test FAILED: the browser exited "
                f"{browser.returncode} after {waited:.0f}s without "
                f"reporting a result. Its last output, from "
                f"{chrome_log}:\n{chrome_log_tail(chrome_log)}"
            )
        if waited > BROWSER_TEST_TIMEOUT_SECONDS:
            sys.exit(
                f"Browser test FAILED: no result after "
                f"{BROWSER_TEST_TIMEOUT_SECONDS}s. The page logs above "
                f"show the last stage it reached; the browser's last "
                f"output, from {chrome_log}:\n"
                f"{chrome_log_tail(chrome_log)}"
            )
    sampler.sample()
    return report


def run_browser_test_phase(
    prefix_root: Path,
    args: argparse.Namespace,
    wheel: Path,
    chrome_bin: Path,
) -> None:
    """Load the wheel in a real browser and assert that
    ``import pyrosetta; pyrosetta.init()`` prints the M1 banner.

    The headless test proves the same thing under Node, and Node shares V8 with
    Chrome, so this is not a second run of the same experiment. What only a
    browser exercises: the wheel arrives over HTTP rather than off the
    filesystem, the module is fetched and compiled by a tab, and everything
    lands in a renderer's memory instead of a process with the machine to
    itself. M1's milestone text asks for a browser, and this is the phase that
    answers it.

    The verdict comes back from the page by POST, so nothing here speaks the
    DevTools protocol and no dependency is added."""
    page = script_dir() / "wasm_browser_test.html"
    if not page.is_file():
        sys.exit(f"The browser test page is missing at {page}")

    pyodide_dist = pyodide_browser_dist_dir(prefix_root)
    if not (pyodide_dist / "pyodide.js").is_file():
        sys.exit(
            f"No browser build of Pyodide at {pyodide_dist}. It ships inside "
            f"the cross-build environment, which Phase 1 installs: delete "
            f"{pyodide_build_install_dir(prefix_root)} and re-run to rebuild "
            f"it."
        )

    absent = missing_chrome_libraries(chrome_bin)
    if absent:
        listed = "\n  ".join(absent)
        sys.exit(
            f"The browser cannot start: {len(absent)} shared libraries it needs "
            f"are missing from this host:\n  {listed}\n"
            f"Install the packages providing them and re-run. On Ubuntu:\n"
            f"  apt-get install -y libasound2t64 libatk-bridge2.0-0t64 "
            f"libatk1.0-0t64 libatspi2.0-0t64 libdbus-1-3 libgbm1 "
            f"libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2"
        )

    results: queue.Queue = queue.Queue()
    token = secrets.token_urlsafe(16)
    server = BrowserTestServer(
        page.read_bytes(), pyodide_dist, wheel, token, results
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    query = urllib.parse.urlencode({"wheel": wheel.name, "token": token})
    url = f"http://127.0.0.1:{server.server_address[1]}/?{query}"

    # The URL carries the result token, and a process's command line is
    # readable by every account on the host — /proc/<pid>/cmdline is
    # world-readable, where a file need not be. So the browser is started on a
    # private bootstrap page that redirects to the real URL, and its command
    # line holds nothing but a path.
    launch_page = build_root(args.type) / "browser-test-launch.html"
    write_private_file(
        launch_page,
        "<!doctype html>\n"
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="refresh" content="0; url={html.escape(url)}">\n'
        "<title>Starting the PyRosetta-WASM browser test</title>\n",
    )

    profile_dir = build_root(args.type) / "browser-test-profile"
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    chrome_log = build_root(args.type) / "browser-test-chrome.log"

    command = [
        str(chrome_bin),
        "--headless",
        "--disable-gpu",
        # A container's /dev/shm is usually 64 MB, and Chrome does not degrade
        # gracefully when it fills.
        "--disable-dev-shm-usage",
        "--no-first-run",
        f"--user-data-dir={profile_dir}",
    ]
    if os.geteuid() == 0:
        # Chrome refuses to run as root with its sandbox on. A host where the
        # build runs unprivileged keeps the sandbox.
        command.append("--no-sandbox")
    command.append(launch_page.as_uri())

    print(
        f"==> Serving {wheel.name} ({wheel.stat().st_size:,} bytes) at "
        f"{redact_token(url)}"
    )
    print(f"    $ {redact_token(' '.join(command))}", flush=True)

    started = time.monotonic()
    with open(chrome_log, "w", encoding="utf-8") as log_file:
        browser = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT)
        sampler = MemorySampler(browser.pid)
        try:
            report = wait_for_browser_result(
                browser, results, sampler, started, chrome_log
            )
        finally:
            browser.terminate()
            try:
                browser.wait(30)
            except subprocess.TimeoutExpired:
                browser.kill()
            server.shutdown()
            server.server_close()
            launch_page.unlink(missing_ok=True)

    elapsed = time.monotonic() - started
    for stage in report.get("marks", []):
        detail = {k: v for k, v in stage.items() if k not in ("name", "seconds")}
        print(f"    {stage.get('seconds', 0):7.1f}s  {stage.get('name', '?')}  {detail}")
    output = report.get("output", "")
    print(output)
    print(
        f"==> Browser run took {elapsed:.0f}s; peak browser memory "
        f"{sampler.peak_proportional / 1e9:.1f} GB proportional, "
        f"{sampler.peak_resident / 1e9:.1f} GB summed resident, "
        f"from {sampler.samples} samples costing {sampler.cost:.1f}s of the run"
    )

    if not report.get("ok"):
        sys.exit(
            f"Browser test FAILED: the page reported an error:\n"
            f"{report.get('error', '(none given)')}\n"
            f"The browser's own output is in {chrome_log}."
        )

    missing = [s for s in SMOKE_TEST_REQUIRED_OUTPUT if s not in output]
    if missing:
        listed = "\n  ".join(repr(s) for s in missing)
        sys.exit(
            f"Browser test FAILED: the page finished, but {len(missing)} of the "
            f"{len(SMOKE_TEST_REQUIRED_OUTPUT)} required substrings are absent "
            f"from what PyRosetta printed:\n  {listed}"
        )

    print(f"Browser test PASSED: {wheel.name} initialises PyRosetta in a browser.")


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
            "M1 banner. "
            "Phase 5 (--browser-test): serve that wheel to a real browser and "
            "assert the same banner."
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
        "--database", default="core", choices=["core", "full"],
        help="How much of the Rosetta database the wheel carries. 'core' "
             "(the default) leaves out the database subtrees outside core "
             "protein modelling, taking the wheel from about 964 MB to "
             "about 172 MB; 'full' ships it whole. Applied to the wheel the "
             "package phase produces, so it does nothing under "
             "--skip-pyodide-build-phase.",
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
        "--browser-test", action="store_true",
        help="Phase 5: run the same assertions in a real browser. Serves the "
             "wheel and the Pyodide runtime on loopback and loads them in a "
             "pinned chrome-headless-shell, downloaded into the toolchain "
             "prefix on first use. Needs Chrome's system libraries on the "
             "host; the phase names any that are missing. Pass both --skip "
             "flags to test an already-built wheel.",
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
        if args.database == "core":
            phases.append("database subset (--database core)")
    if args.test:
        phases.append("test (headless smoke test)")
    if args.browser_test:
        phases.append("browser test (chrome-headless-shell)")
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
        if (args.test or args.browser_test) and wheel is None:
            sys.exit(
                "The package phase produced no wheel to smoke-test. Falling "
                "back to whatever dist/ still holds would report a pass for "
                "a build that made nothing."
            )
        if wheel is not None and args.database == "core":
            wheel = write_core_database_wheel(wheel)

    if (args.test or args.browser_test) and wheel is None:
        wheel = find_wheel_to_test(wheel_dist_dir(args.type))

    # Phase 4.
    if args.test:
        run_test_phase(prefix, pyodide_venv_bin, emsdk_env, args, wheel)

    # Phase 5.
    if args.browser_test:
        chrome_bin = install_chrome_headless_shell(prefix)
        run_browser_test_phase(prefix, args, wheel, chrome_bin)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
