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

Sibling to build.py. Does not modify it. See
``.ai/adrs/0001-pyodide-integration.md`` for the full design.

Pipeline (task 0005):

1. Phase 1 — install toolchain (uv, emsdk, pyodide-build) into
   ``source/build/PyRosetta-WASM/prefix/``.
2. Phase 2 — generation: shell out to ``build.py --skip-building-phase``
   to run Binder and produce ``setup.py``. Skipped by
   ``--skip-generation-phase``.
3. Phase 3 — build: invoke ``pyodide build`` against the generated
   ``setup.py``. Skipped by ``--skip-building-phase``.

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
# Generation phase — shell out to build.py --skip-building-phase.
# ---------------------------------------------------------------------------
def discover_inner_build_root(build_type: str) -> Path:
    """Ask the inner build.py where it will write generation output.
    The returned path is the binding build root; ``setup.py`` lives at
    ``<root>/build/setup.py`` once the generation phase has run."""
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


def run_generation_phase(args: argparse.Namespace) -> Path:
    """Run ``build.py --skip-building-phase`` and return the inner
    binding build root (the directory whose ``build/`` contains
    ``setup.py`` afterwards)."""
    inner_root = discover_inner_build_root(args.type)

    cmd = [
        sys.executable, "build.py",
        "--skip-building-phase",
        "--target", "wasm",
        "--type", args.type,
        "-j", str(args.jobs),
    ]
    if args.version_file:
        cmd += ["--version", args.version_file]

    execute(
        "Running inner build.py generation phase (Binder + CMake configure)",
        *cmd,
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
) -> Path:
    """Invoke ``pyodide build`` on ``<inner_build_root>/build/``. Returns
    the outdir where wheels (if any) land."""
    setup_dir = inner_build_root / "build"
    setup_py = setup_dir / "setup.py"
    if not setup_py.is_file():
        sys.exit(
            f"Expected setup.py at {setup_py} after the generation "
            f"phase, but it is missing. The inner build.py may have "
            f"changed; re-check the handoff path."
        )

    outdir = build_root(args.type) / "dist"
    outdir.mkdir(parents=True, exist_ok=True)

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

    wheels = sorted(outdir.glob("*.whl"))
    if wheels:
        print()
        print(f"Wheels produced under {outdir}:")
        for w in wheels:
            print(f"  {w.name}")
    else:
        print()
        print(f"WARNING: pyodide build exited 0 but no wheel was found in {outdir}")
    return outdir


# ---------------------------------------------------------------------------
# CLI / main.
# ---------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build PyRosetta for WebAssembly (Pyodide). Sibling of build.py. "
            "Phase 1: auto-install the WASM toolchain (uv, emsdk, pyodide-build) "
            "under source/build/PyRosetta-WASM/prefix/. "
            "Phase 2: shell out to build.py --skip-building-phase for Binder + "
            "CMake generation. "
            "Phase 3: invoke `pyodide build` against the generated setup.py."
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
        "--skip-generation-phase", action="store_true",
        help="Skip Phase 2 (inner build.py Binder/CMake generation). "
             "Useful when iterating on Phase 3 with an already-generated "
             "source tree.",
    )
    parser.add_argument(
        "--skip-building-phase", action="store_true",
        help="Skip Phase 3 (pyodide build). With this flag the script "
             "stops after the toolchain install (and generation, unless "
             "--skip-generation-phase is also set).",
    )
    parser.add_argument(
        "--print-build-root", action="store_true",
        help="Print the WASM build root path and exit.",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="(Future task) After building, run the headless smoke test.",
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
    if not args.skip_generation_phase:
        phases.append("generation (build.py --skip-building-phase)")
    if not args.skip_building_phase:
        phases.append("build (pyodide build)")
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
    if args.skip_generation_phase:
        print("Skipping generation phase (--skip-generation-phase).")
        inner_build_root = discover_inner_build_root(args.type)
    else:
        inner_build_root = run_generation_phase(args)

    # Phase 3.
    if args.skip_building_phase:
        print("Skipping build phase (--skip-building-phase). Done.")
        return 0

    run_pyodide_build_phase(
        prefix, pyodide_venv_bin, emsdk_env, args, inner_build_root
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
