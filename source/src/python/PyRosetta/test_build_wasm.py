#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# :noTabs=true:

# (c) Copyright Rosetta Commons Member Institutions.
# (c) This file is part of the Rosetta software suite and is made available under license.
# (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
# (c) For more information, see http://www.rosettacommons.org. Questions about this can be
# (c) addressed to University of Washington CoMotion, email: license@uw.edu.

"""Unit tests for build-wasm.py.

Run with::

    python3 source/src/python/PyRosetta/test_build_wasm.py

The module under test is hyphenated, so it cannot be imported by name and is
loaded through importlib instead.
"""

from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
import queue
import shlex
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock


def load_build_wasm():
    path = Path(__file__).resolve().parent / "build-wasm.py"
    spec = importlib.util.spec_from_file_location("build_wasm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_wasm = load_build_wasm()


class CoreDatabaseKeepsTest(unittest.TestCase):
    """The predicate that decides what the core wheel's database holds."""

    def test_keeps_a_score_function_the_traced_workload_read(self):
        self.assertTrue(
            build_wasm.core_database_keeps(
                "scoring/score_functions/rama/fd/all.ramaProb"
            )
        )

    def test_keeps_the_fa_standard_residue_types(self):
        self.assertTrue(
            build_wasm.core_database_keeps(
                "chemical/residue_type_sets/fa_standard/residue_types.txt"
            )
        )

    def test_drops_the_fragment_libraries(self):
        self.assertFalse(
            build_wasm.core_database_keeps("sampling/filtered.vall.dat.2006-05-05.gz")
        )

    def test_drops_the_pdb_chemical_component_dictionary(self):
        self.assertFalse(
            build_wasm.core_database_keeps("chemical/pdb_components/components.A.cif")
        )

    def test_keeps_the_pdb_components_override_file(self):
        """GlobalResidueTypeSet.cc:901 calls utility_exit_with_message when this
        93-byte file is missing, on the first PDB residue it does not recognise.
        It sits inside the 95 MB subtree the .cif files are dropped from."""
        self.assertTrue(
            build_wasm.core_database_keeps("chemical/pdb_components/override.txt")
        )

    def test_keeps_the_sasa_lookup_tables(self):
        """core/scoring/sasa.cc:110,130 streams these into fixed arrays without
        checking good(), so dropping them does not fail — it silently computes
        every SASA against a zero-filled table."""
        for table in ["sampling/SASA-masks.dat", "sampling/SASA-angles.dat"]:
            with self.subTest(table=table):
                self.assertTrue(build_wasm.core_database_keeps(table))

    def test_keeps_the_relax_scripts(self):
        """RelaxScriptManager.cc:168 exits without these, so FastRelax cannot
        run. They are 26 KB inside the 264 MB of fragment libraries."""
        self.assertTrue(
            build_wasm.core_database_keeps("sampling/relax_scripts/MonomerRelax2019.txt")
        )

    def test_drops_the_vall_fragment_libraries(self):
        for vall in ["sampling/filtered.vall.dat.2006-05-05.gz",
                     "sampling/vall.jul19.2011.torsions.gz",
                     "sampling/vall.dat.2001-02-02.torsions.gz",
                     "sampling/small.vall.gz"]:
            with self.subTest(vall=vall):
                self.assertFalse(build_wasm.core_database_keeps(vall))

    def test_keeps_the_beta_nov2016_rotamers(self):
        """-beta / -beta_nov16 is a mainstream score function, and
        score_function_corrections.cc:1971 points dun10_dir here when it is passed."""
        self.assertTrue(
            build_wasm.core_database_keeps("rotamer/beta_nov2016/gln.bbdep.rotamers.lib.gz")
        )

    def test_keeps_the_default_dunbrack_smoothing_level(self):
        self.assertTrue(
            build_wasm.core_database_keeps(
                "rotamer/shapovalov/StpDwn_0-0-0/arg.bbdep.rotamers.lib.gz"
            )
        )

    def test_drops_the_other_dunbrack_smoothing_levels(self):
        for level in ["StpDwn_2-2-2", "StpDwn_5-5-5", "StpDwn_10-10-10",
                      "StpDwn_20-20-20", "StpDwn_25-25-25"]:
            with self.subTest(level=level):
                self.assertFalse(
                    build_wasm.core_database_keeps(
                        f"rotamer/shapovalov/{level}/arg.bbdep.rotamers.lib.gz"
                    )
                )

    def test_drops_both_copies_of_the_2002_dunbrack_library(self):
        self.assertFalse(build_wasm.core_database_keeps("rotamer/bbdep02.May.sortlib"))
        self.assertFalse(
            build_wasm.core_database_keeps(
                "rotamer/bbdep02.May.sortlib-correct.12.2010"
            )
        )

    def test_drops_every_mhc_sequence_subtree(self):
        for subtree in ["mhc_pssms", "mhc_rank_svm_scores", "mhc_svms"]:
            with self.subTest(subtree=subtree):
                self.assertFalse(
                    build_wasm.core_database_keeps(f"sequence/{subtree}/anything.txt")
                )

    def test_keeps_a_sequence_subtree_that_is_not_listed(self):
        """The mhc_ entry is a name prefix, not a whole-directory match, so
        this pins that it does not reach its siblings."""
        self.assertTrue(
            build_wasm.core_database_keeps("sequence/substitution_matrix/BLOSUM62")
        )

    def test_keeps_a_path_that_only_shares_a_prefix_with_a_dropped_subtree(self):
        self.assertTrue(build_wasm.core_database_keeps("scoring/rnase_notreal/x.txt"))

    def test_drops_the_score_terms_outside_core_modelling(self):
        """The second pass, inside scoring/score_functions. Every one of these
        is reached only when something outside core protein modelling asks for
        it, and every one fails by naming the file it could not open."""
        for path in [
            "scoring/score_functions/goap/angle_table.dat.gz",
            "scoring/score_functions/mhc_epitope/iedb_data.db",
            "scoring/score_functions/rama/Rama08.dat",
            "scoring/score_functions/rama/aramid/KAA_rama.txt",
            "scoring/score_functions/rama/beta_aa/B3A.rama",
            "scoring/score_functions/rama/oligourea/OU3_ALA_rama.txt",
        ]:
            with self.subTest(path=path):
                self.assertFalse(build_wasm.core_database_keeps(path))

    def test_drops_the_two_bead_etables(self):
        """No code reads these: no option default names them, and the only
        mention in the tree is a line in source/src/utility/file_list, a
        manifest nothing consumes."""
        self.assertFalse(
            build_wasm.core_database_keeps(
                "scoring/score_functions/etable/etable.twobead.lj.dat"
            )
        )

    def test_drops_the_finer_p_aa_pp_propensity_grid(self):
        self.assertFalse(
            build_wasm.core_database_keeps(
                "scoring/score_functions/P_AA_pp/shapovalov/2.5deg/kappa50/a20.prop"
            )
        )

    def test_keeps_both_p_aa_pp_smoothing_levels_a_flag_can_select(self):
        """score_function_corrections.cc:676,679 sets shap_p_aa_pp to kappa131
        for -shap_p_aa_pp_smooth_level 1 and kappa50 for 2, both under 10deg/.
        Level 1 is the default (options_rosetta.py:2811), so kappa131 is what
        a default-flags run actually reads."""
        for kappa in ["kappa50", "kappa131"]:
            with self.subTest(kappa=kappa):
                self.assertTrue(
                    build_wasm.core_database_keeps(
                        "scoring/score_functions/P_AA_pp/shapovalov/10deg/"
                        f"{kappa}/a20.prop"
                    )
                )

    def test_keeps_the_rama_maps_a_default_run_reads(self):
        """rama/Rama08.dat is dropped by exact name, so this pins that it does
        not reach the default rama_map or shap_rama_map beside it. The
        rotamer/shapovalov/ special case is a different tree from this one."""
        for path in [
            "scoring/score_functions/rama/Rama_smooth_dyn.dat_ss_6.4",
            "scoring/score_functions/rama/shapovalov/kappa25/all.ramaProb",
        ]:
            with self.subTest(path=path):
                self.assertTrue(build_wasm.core_database_keeps(path))


def build_wheel(path: Path, entries: dict[str, bytes], with_record: bool = True) -> None:
    """Write a minimal wheel holding exactly ``entries``."""
    rows = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for name, payload in entries.items():
            wheel.writestr(name, payload)
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(payload).digest()
            ).rstrip(b"=").decode("ascii")
            rows.append((name, f"sha256={digest}", len(payload)))
        if with_record:
            record = io.StringIO()
            writer = csv.writer(record, lineterminator="\n")
            writer.writerows(rows)
            writer.writerow(("pyrosetta-0.0.dist-info/RECORD", "", ""))
            wheel.writestr("pyrosetta-0.0.dist-info/RECORD", record.getvalue())


class WriteCoreDatabaseWheelTest(unittest.TestCase):
    """Rewriting a wheel with the dropped database subtrees removed."""

    ENTRIES = {
        "pyrosetta/__init__.py": b"import rosetta\n",
        "pyrosetta/rosetta.so": b"\x00asm" + b"payload" * 100,
        "pyrosetta/database/chemical/residue_type_sets/fa_standard/x.txt": b"keep me\n",
        "pyrosetta/database/scoring/score_functions/rama/fd/all.ramaProb": b"keep\n",
        "pyrosetta/database/rotamer/shapovalov/StpDwn_0-0-0/arg.lib.gz": b"keep\n",
        "pyrosetta/database/rotamer/shapovalov/StpDwn_5-5-5/arg.lib.gz": b"drop\n",
        "pyrosetta/database/sampling/vall.gz": b"drop me, i am large\n",
        "pyrosetta/database/sampling/SASA-masks.dat": b"keep, inside a dropped tree\n",
        "pyrosetta/database/sampling/antibodies/L3-10-cis7,8-1.txt": b"drop, comma\n",
        "pyrosetta/database/external/svm_models/model.dat": b"drop\n",
        "pyrosetta-0.0.dist-info/METADATA": b"Name: pyrosetta\n",
    }

    KEPT = [
        "pyrosetta/__init__.py",
        "pyrosetta/rosetta.so",
        "pyrosetta/database/chemical/residue_type_sets/fa_standard/x.txt",
        "pyrosetta/database/scoring/score_functions/rama/fd/all.ramaProb",
        "pyrosetta/database/rotamer/shapovalov/StpDwn_0-0-0/arg.lib.gz",
        "pyrosetta/database/sampling/SASA-masks.dat",
        "pyrosetta-0.0.dist-info/METADATA",
    ]

    DROPPED = [
        "pyrosetta/database/rotamer/shapovalov/StpDwn_5-5-5/arg.lib.gz",
        "pyrosetta/database/sampling/vall.gz",
        "pyrosetta/database/sampling/antibodies/L3-10-cis7,8-1.txt",
        "pyrosetta/database/external/svm_models/model.dat",
    ]

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.wheel = Path(self.directory.name) / "pyrosetta-0.0-py3-none-any.whl"
        build_wheel(self.wheel, self.ENTRIES)

    def test_drops_the_excluded_database_files_and_keeps_the_rest(self):
        build_wasm.write_core_database_wheel(self.wheel)
        with zipfile.ZipFile(self.wheel) as wheel:
            names = set(wheel.namelist())
        self.assertEqual(
            names, set(self.KEPT) | {"pyrosetta-0.0.dist-info/RECORD"}
        )

    def test_kept_files_survive_byte_for_byte(self):
        build_wasm.write_core_database_wheel(self.wheel)
        with zipfile.ZipFile(self.wheel) as wheel:
            for name in self.KEPT:
                with self.subTest(name=name):
                    self.assertEqual(wheel.read(name), self.ENTRIES[name])

    def test_rewrites_the_wheel_in_place(self):
        returned = build_wasm.write_core_database_wheel(self.wheel)
        self.assertEqual(returned, self.wheel)
        self.assertEqual(
            sorted(p.name for p in self.wheel.parent.iterdir()),
            [self.wheel.name],
            "the repack left a stray file next to the wheel",
        )

    def test_record_lists_exactly_the_kept_files_with_correct_hashes(self):
        build_wasm.write_core_database_wheel(self.wheel)
        with zipfile.ZipFile(self.wheel) as wheel:
            text = wheel.read("pyrosetta-0.0.dist-info/RECORD").decode("utf-8")
            rows = list(csv.reader(io.StringIO(text)))
        listed = {row[0]: row for row in rows}
        self.assertEqual(
            set(listed), set(self.KEPT) | {"pyrosetta-0.0.dist-info/RECORD"}
        )
        for name in self.KEPT:
            with self.subTest(name=name):
                payload = self.ENTRIES[name]
                digest = base64.urlsafe_b64encode(
                    hashlib.sha256(payload).digest()
                ).rstrip(b"=").decode("ascii")
                self.assertEqual(listed[name][1], f"sha256={digest}")
                self.assertEqual(listed[name][2], str(len(payload)))

    def test_record_carries_no_hash_or_size_for_itself(self):
        build_wasm.write_core_database_wheel(self.wheel)
        with zipfile.ZipFile(self.wheel) as wheel:
            text = wheel.read("pyrosetta-0.0.dist-info/RECORD").decode("utf-8")
            rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[-1], ["pyrosetta-0.0.dist-info/RECORD", "", ""])

    def test_a_comma_in_a_path_round_trips_through_record(self):
        """Four real database paths contain a comma, so RECORD has to quote
        them rather than gain a column."""
        entries = dict(self.ENTRIES)
        entries["pyrosetta/database/scoring/L3-10-cis7,8-1.txt"] = b"keep, comma\n"
        build_wheel(self.wheel, entries)
        build_wasm.write_core_database_wheel(self.wheel)
        with zipfile.ZipFile(self.wheel) as wheel:
            text = wheel.read("pyrosetta-0.0.dist-info/RECORD").decode("utf-8")
            rows = list(csv.reader(io.StringIO(text)))
        commas = [r for r in rows if r[0].endswith("L3-10-cis7,8-1.txt")]
        self.assertEqual(len(commas), 1, f"expected one row, got {rows}")
        self.assertEqual(len(commas[0]), 3)

    def test_a_wheel_without_a_record_is_refused(self):
        build_wheel(self.wheel, self.ENTRIES, with_record=False)
        with self.assertRaises(SystemExit) as raised:
            build_wasm.write_core_database_wheel(self.wheel)
        self.assertIn("RECORD", str(raised.exception))

    def test_a_refused_wheel_is_left_untouched(self):
        build_wheel(self.wheel, self.ENTRIES, with_record=False)
        before = self.wheel.read_bytes()
        with self.assertRaises(SystemExit):
            build_wasm.write_core_database_wheel(self.wheel)
        self.assertEqual(self.wheel.read_bytes(), before)
        self.assertEqual(
            sorted(p.name for p in self.wheel.parent.iterdir()), [self.wheel.name]
        )


class FakeBrowser:
    """Stands in for the ``subprocess.Popen`` of a browser.

    ``wait_for_browser_result`` only ever asks a browser for its pid and
    whether it has exited."""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class ReportAfter:
    """A results queue that is empty for the first `polls` gets, then answers.

    Stands in for the real queue so a test can count polls without waiting a
    second for each one."""

    def __init__(self, polls: int, report: dict) -> None:
        self.polls = polls
        self.report = report

    def get(self, timeout: float) -> dict:
        if self.polls > 0:
            self.polls -= 1
            raise queue.Empty
        return self.report


class MemorySamplerTest(unittest.TestCase):
    """What the browser test's memory sampler keeps and what it records."""

    def test_keeps_the_largest_reading_not_the_latest(self):
        """The maximum can fall anywhere in the run — interior for a wheel big
        enough that unlinking the archive matters — so a later, smaller
        reading must not replace it."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        readings = [(6_000_000_000, 6_300_000_000), (1_000, 2_000)]
        with mock.patch.object(
            build_wasm, "process_tree_memory", side_effect=readings
        ):
            sampler.sample()
            sampler.sample()
        self.assertEqual(sampler.peak_proportional, 6_000_000_000)
        self.assertEqual(sampler.peak_resident, 6_300_000_000)
        self.assertEqual(sampler.samples, 2)

    def test_adds_up_what_the_samples_cost(self):
        """The phase prints this, because it grows with the memory measured and
        is the perturbation the figure carries."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())

        def slow_reading(pid):
            time.sleep(0.05)
            return 1, 1

        with mock.patch.object(build_wasm, "process_tree_memory", slow_reading):
            sampler.sample()
            sampler.sample()
        self.assertGreaterEqual(sampler.cost, 0.1)

    def test_charges_readings_to_the_stage_the_page_reached(self):
        """A stage's peak covers the polls taken while the page worked towards
        it, plus the boundary reading `reached` takes on arrival."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        readings = [
            (1_000, 1_100),  # polled while the wheel streamed in
            (9_000, 9_100),  # polled again, still streaming
            (8_000, 8_100),  # the boundary reading at wheel-fetched
            (3_000, 3_100),  # polled while micropip unpacked
            (7_000, 7_100),  # the boundary reading at wheel-installed
        ]
        with mock.patch.object(
            build_wasm, "process_tree_memory", side_effect=readings
        ):
            sampler.sample()
            sampler.sample()
            sampler.reached("wheel-fetched")
            sampler.sample()
            sampler.reached("wheel-installed")
        self.assertEqual(
            {name: (p.proportional, p.resident) for name, p in sampler.peaks.items()},
            {
                "wheel-fetched": (9_000, 9_100),
                "wheel-installed": (7_000, 7_100),
            },
        )

    def test_a_stage_does_not_inherit_the_peak_of_an_earlier_one(self):
        """Attributing the run is the point, so a stage reports what it cost
        and not the high-water mark of everything before it — while the
        headline figure stays the largest reading of the whole run."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        readings = [(6_000_000_000, 6_300_000_000), (1_000_000_000, 1_100_000_000)]
        with mock.patch.object(
            build_wasm, "process_tree_memory", side_effect=readings
        ):
            sampler.reached("wheel-installed")
            sampler.reached("pyrosetta-imported")
        imported = sampler.peaks["pyrosetta-imported"]
        self.assertEqual(
            (imported.proportional, imported.resident),
            (1_000_000_000, 1_100_000_000),
        )
        self.assertEqual(sampler.peak_proportional, 6_000_000_000)
        self.assertEqual(sampler.peak_resident, 6_300_000_000)

    def test_times_a_stage_from_when_the_browser_was_launched(self):
        """The page's marks are relative to its own script starting, which is
        after however long the browser took to get there — so the table's one
        time column has to come from the harness's clock instead."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic() - 12.0)
        with mock.patch.object(
            build_wasm, "process_tree_memory", lambda pid: (1, 1)
        ):
            sampler.reached("pyodide-booted")
        self.assertAlmostEqual(
            sampler.peaks["pyodide-booted"].seconds, 12.0, places=1
        )

    def test_counts_a_reading_taken_after_the_last_stage(self):
        """The headline peak is the largest of everything sampled, whether or
        not a stage has closed over it yet."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        readings = [(1_000, 1_100), (5_000, 5_100)]
        with mock.patch.object(
            build_wasm, "process_tree_memory", side_effect=readings
        ):
            sampler.reached("wheel-fetched")
            sampler.sample()
        fetched = sampler.peaks["wheel-fetched"]
        self.assertEqual(list(sampler.peaks), ["wheel-fetched"])
        self.assertEqual((fetched.proportional, fetched.resident), (1_000, 1_100))
        self.assertEqual(sampler.peak_proportional, 5_000)
        self.assertEqual(sampler.peak_resident, 5_100)

    def test_reads_the_memory_of_a_real_process_tree(self):
        """No mock: this exercises /proc against the interpreter running the
        test, which is the only process tree a unit test can count on."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        sampler.sample()
        # A CPython process holds megabytes, so this is well clear of the zero
        # that a /proc read gone wrong would report.
        self.assertGreater(sampler.peak_proportional, 1_000_000)
        self.assertGreater(sampler.peak_resident, 1_000_000)


class BrowserTestHandlerTest(unittest.TestCase):
    """What the handler lets onto the results queue.

    `wait_for_browser_result` tells a stage name from a verdict by type, and
    that is only sound because nothing but a JSON object can reach the queue.
    Nothing states that rule directly — it falls out of the token check, which
    was written for a different purpose — so it is pinned here.
    """

    TOKEN = "the-run-token"

    def setUp(self):
        self.results = queue.Queue()
        server = build_wasm.BrowserTestServer(
            b"<!doctype html>",
            Path("/nonexistent"),
            Path("/nonexistent/wheel.whl"),
            self.TOKEN,
            self.results,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, path, payload):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as refused:
            return refused.code

    def test_a_mark_reaches_the_queue_as_text(self):
        status = self.post(
            "/mark", {"token": self.TOKEN, "name": "wheel-fetched", "detail": {}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.results.get_nowait(), "wheel-fetched")

    def test_a_verdict_reaches_the_queue_as_an_object(self):
        status = self.post(
            "/result", {"token": self.TOKEN, "ok": True, "marks": []}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.results.get_nowait(), {"token": self.TOKEN, "ok": True, "marks": []}
        )

    def test_a_payload_that_is_not_an_object_never_reaches_the_queue(self):
        """This is the invariant the poll loop's type test depends on: a bare
        JSON string posted to /result would otherwise read as a stage name and
        the run would never end."""
        for payload in ["just a string", ["a", "b"], 42, None]:
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/result", payload), 403)
        self.assertTrue(self.results.empty())

    def test_a_wrong_token_never_reaches_the_queue(self):
        self.assertEqual(
            self.post("/result", {"token": "wrong", "ok": True}), 403
        )
        self.assertEqual(
            self.post("/mark", {"token": "wrong", "name": "wheel-fetched"}), 403
        )
        self.assertTrue(self.results.empty())


class AlwaysMarks:
    """A results queue that hands back a stage name and never runs dry.

    Stands in for a page that marks faster than the poll interval — which no
    page does today, but `fetchWheelIntoFilesystem` already logs once per
    250 MB, so a mark in that loop is one edit away.

    Bounded, because the loop it feeds has no other way out: left unbounded, a
    regression would hang the suite rather than fail it, exactly when the test
    is doing its job."""

    def __init__(self, patience: int = 10_000) -> None:
        self.patience = patience

    def get(self, timeout: float) -> str:
        self.patience -= 1
        if self.patience < 0:
            raise AssertionError(
                "wait_for_browser_result took 10,000 stage names without "
                "checking the browser or the clock: the guards are reachable "
                "only when a poll times out"
            )
        return "wheel-fetched"


class PrintableTest(unittest.TestCase):
    """What the harness will put in a build log on the page's say-so."""

    def test_escapes_control_characters(self):
        """A traceback carrying an escape sequence would otherwise rewrite the
        terminal around the line it is printed on."""
        self.assertEqual(
            build_wasm.BrowserTestHandler.printable("a\x1b[2Jb\n"),
            "a\\x1b[2Jb\\x0a",
        )

    def test_truncates_at_the_limit_it_is_given(self):
        """A stage name becomes a column in the summary table, so it passes a
        tight limit; a log message keeps the generous default, because a
        truncated traceback is the one the phase most needs whole."""
        self.assertEqual(
            build_wasm.BrowserTestHandler.printable("x" * 500, limit=64),
            "x" * 64,
        )
        self.assertEqual(
            build_wasm.BrowserTestHandler.printable("x" * 500), "x" * 500
        )

    def test_blanks_a_run_token(self):
        """The page's URL carries the run token and a stack trace names the
        document it threw in, so a message from the page can quote it into a
        build log."""
        message = "at run (http://127.0.0.1:41234/?wheel=w.whl&token=SEKRIT:9)"
        printed = build_wasm.BrowserTestHandler.printable(message)
        self.assertNotIn("SEKRIT", printed)
        self.assertIn("token=<redacted>", printed)

    def test_renders_a_value_that_is_not_text(self):
        """`json.loads` will hand back whatever the page posted, and a missing
        key arrives as None."""
        self.assertEqual(build_wasm.BrowserTestHandler.printable(None), "None")


class WaitForBrowserResultTest(unittest.TestCase):
    """The loop that waits out a browser run and samples it."""

    MISSING_LOG = Path("/nonexistent/browser-test-chrome.log")

    def test_takes_a_final_sample_once_the_verdict_is_in(self):
        """A verdict waiting in the queue is returned without a single poll, so
        the one sample taken here is the final one and nothing else."""
        results = queue.Queue()
        results.put({"ok": True})
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        with mock.patch.object(
            build_wasm, "process_tree_memory", lambda pid: (7, 9)
        ):
            report = build_wasm.wait_for_browser_result(
                FakeBrowser(os.getpid()),
                results,
                sampler,
                time.monotonic(),
                self.MISSING_LOG,
            )
        self.assertEqual(report, {"ok": True})
        self.assertEqual(sampler.samples, 1)
        self.assertEqual(sampler.peak_proportional, 7)

    def test_samples_on_every_poll_it_waits_out(self):
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        with mock.patch.object(
            build_wasm, "process_tree_memory", lambda pid: (1, 1)
        ):
            build_wasm.wait_for_browser_result(
                FakeBrowser(os.getpid()),
                ReportAfter(3, {"ok": True}),
                sampler,
                time.monotonic(),
                self.MISSING_LOG,
            )
        self.assertEqual(sampler.samples, 4, "three polls, then the verdict")

    def test_a_stage_name_is_not_a_verdict_and_does_not_end_the_wait(self):
        """The page posts stage names and its verdict on the same queue, so the
        loop has to keep waiting through the names and group by them."""
        results = queue.Queue()
        results.put("wheel-fetched")
        results.put("wheel-installed")
        results.put({"ok": True, "marks": []})
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        with mock.patch.object(
            build_wasm, "process_tree_memory", side_effect=[(1, 1), (2, 2), (3, 3)]
        ):
            report = build_wasm.wait_for_browser_result(
                FakeBrowser(os.getpid()),
                results,
                sampler,
                time.monotonic(),
                self.MISSING_LOG,
            )
        self.assertEqual(report, {"ok": True, "marks": []})
        self.assertEqual(
            list(sampler.peaks), ["wheel-fetched", "wheel-installed", "verdict"]
        )

    def test_a_stream_of_stage_names_does_not_starve_the_timeout(self):
        """Stage names and the verdict share a queue, so a page that marks
        without pause never leaves the loop through `queue.Empty`. The timeout
        must not be reachable only on that path."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        overran = time.monotonic() - build_wasm.BROWSER_TEST_TIMEOUT_SECONDS - 1
        with mock.patch.object(
            build_wasm, "process_tree_memory", lambda pid: (1, 1)
        ):
            with self.assertRaises(SystemExit) as raised:
                build_wasm.wait_for_browser_result(
                    FakeBrowser(os.getpid()),
                    AlwaysMarks(),
                    sampler,
                    overran,
                    self.MISSING_LOG,
                )
        self.assertIn("no result after", str(raised.exception))

    def test_a_stream_of_stage_names_does_not_hide_a_dead_browser(self):
        """The same path guards the other check: a browser that died still has
        to be noticed while marks are arriving."""
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        with mock.patch.object(
            build_wasm, "process_tree_memory", lambda pid: (1, 1)
        ):
            with self.assertRaises(SystemExit) as raised:
                build_wasm.wait_for_browser_result(
                    FakeBrowser(os.getpid(), returncode=9),
                    AlwaysMarks(),
                    sampler,
                    time.monotonic(),
                    self.MISSING_LOG,
                )
        self.assertIn("the browser exited 9", str(raised.exception))

    def test_a_browser_that_exits_without_reporting_fails_the_phase(self):
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        with mock.patch.object(
            build_wasm, "process_tree_memory", lambda pid: (1, 1)
        ):
            with self.assertRaises(SystemExit) as raised:
                build_wasm.wait_for_browser_result(
                    FakeBrowser(os.getpid(), returncode=9),
                    ReportAfter(5, {"ok": True}),
                    sampler,
                    time.monotonic(),
                    self.MISSING_LOG,
                )
        self.assertIn("the browser exited 9", str(raised.exception))

    def test_a_run_past_the_timeout_fails_the_phase(self):
        sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())
        overran = time.monotonic() - build_wasm.BROWSER_TEST_TIMEOUT_SECONDS - 1
        with mock.patch.object(
            build_wasm, "process_tree_memory", lambda pid: (1, 1)
        ):
            with self.assertRaises(SystemExit) as raised:
                build_wasm.wait_for_browser_result(
                    FakeBrowser(os.getpid()),
                    ReportAfter(5, {"ok": True}),
                    sampler,
                    overran,
                    self.MISSING_LOG,
                )
        self.assertIn("no result after", str(raised.exception))


# What `pyrosetta.init()` prints, cut down to the lines the M1 assertions need.
BANNER = (
    "┌" + "─" * 79 + "┐\n"
    "│" + "PyRosetta-4".center(79) + "│\n"
    "└" + "─" * 79 + "┘\n"
    "core.init: Rosetta version: 0.0.dev0\n"
    "basic.random.init_random_generator: RandomGenerator:init: Normal mode\n"
)


def stdout(text):
    return {"output_type": "stream", "name": "stdout", "text": text}


class NotebookReportFailureTest(unittest.TestCase):
    """The verdict on a notebook run through the site's kernel."""

    def test_passes_when_every_cell_succeeds_and_the_banner_was_printed(self):
        report = {
            "cells": [
                {"id": "install", "status": "ok", "outputs": []},
                {"id": "init", "status": "ok", "outputs": [stdout(BANNER)]},
            ]
        }
        self.assertIsNone(build_wasm.notebook_report_failure(report))

    def test_passes_a_run_whose_browser_only_cell_was_skipped(self):
        report = {
            "cells": [
                {"id": "init", "status": "ok", "outputs": [stdout(BANNER)]},
                {"id": "rcsb", "status": "skipped", "outputs": []},
            ]
        }
        self.assertIsNone(build_wasm.notebook_report_failure(report))

    def test_names_the_cell_that_raised_and_its_exception(self):
        report = {
            "cells": [
                {"id": "init", "status": "ok", "outputs": [stdout(BANNER)]},
                {
                    "id": "score",
                    "status": "error",
                    "ename": "<class 'RuntimeError'>",
                    "evalue": "memory access out of bounds",
                    "traceback": ["Traceback (most recent call last):"],
                    "outputs": [],
                },
            ]
        }
        failure = build_wasm.notebook_report_failure(report)
        self.assertIn("cell score", failure)
        self.assertIn("RuntimeError", failure)
        self.assertIn("memory access out of bounds", failure)

    def test_a_traceback_reaches_the_log_without_its_control_characters(self):
        """IPython colours every traceback, and a cell's code controls the
        rest of the text, which can carry sequences that rewrite a terminal."""
        report = {
            "cells": [
                {
                    "id": "score",
                    "status": "error",
                    "ename": "<class 'ZeroDivisionError'>",
                    "evalue": "division by zero",
                    "traceback": [
                        "\x1b[31mZeroDivisionError\x1b[39m  Traceback",
                        "\x1b]0;retitled\x07----> 1 1/0",
                    ],
                    "outputs": [],
                }
            ]
        }
        failure = build_wasm.notebook_report_failure(report)
        self.assertNotIn("\x1b", failure)
        self.assertNotIn("\x07", failure)
        self.assertIn("ZeroDivisionError  Traceback\n", failure)
        self.assertIn("\\x1b]0;retitled\\x07----> 1 1/0", failure)

    def test_fails_when_the_tracer_never_reached_a_cell(self):
        """What a muted tracer with no logging route looks like from the
        notebook's side: the box is a Python print and still arrives, while
        the C++ tracer's lines do not."""
        box = "".join(BANNER.splitlines(keepends=True)[:3])
        report = {"cells": [{"id": "init", "status": "ok", "outputs": [stdout(box)]}]}
        failure = build_wasm.notebook_report_failure(report)
        self.assertIn("required substrings are absent", failure)
        self.assertIn("core.init:", failure)
        self.assertIn("basic.random.init_random_generator:", failure)
        self.assertNotIn("'PyRosetta-4'", failure)

    def test_finds_the_banner_across_separate_stream_writes(self):
        """The kernel's streams deliver whatever each write handed them:
        `print` writes its text and its newline separately, and `logging`
        writes one record at a time."""
        lines = BANNER.splitlines(keepends=True)
        report = {
            "cells": [
                {"id": "import", "status": "ok", "outputs": [stdout(lines[0])]},
                {
                    "id": "init",
                    "status": "ok",
                    "outputs": [stdout(line) for line in lines[1:]],
                },
            ]
        }
        self.assertIsNone(build_wasm.notebook_report_failure(report))

    def test_a_run_with_no_cells_fails(self):
        """A notebook with no code cells must not pass by asserting nothing."""
        self.assertIsNotNone(build_wasm.notebook_report_failure({"cells": []}))


class JupyterLiteConfigTest(unittest.TestCase):
    """The configuration the JupyterLite site is built from."""

    def test_the_kernel_loads_the_pyodide_release_the_wheel_was_built_for(self):
        """The notebook test loads this release from the cross-build
        environment, standing in for the CDN copy the site names; the stand-in
        is only faithful while the two are the same release."""
        settings = build_wasm.jupyterlite_config()["jupyter-config-data"][
            "litePluginSettings"
        ]["@jupyterlite/pyodide-kernel-extension:kernel"]
        self.assertEqual(
            settings["pyodideUrl"],
            f"https://cdn.jsdelivr.net/pyodide/v{build_wasm.PYODIDE_VERSION}"
            f"/full/pyodide.js",
        )


class JupyterLiteBuildEnvTest(unittest.TestCase):
    """The environment the site is built in."""

    def test_drops_the_variables_that_would_change_what_the_site_ships(self):
        env = build_wasm.jupyterlite_build_env(
            {
                "JUPYTERLITE_PYODIDE_URL": "https://example.invalid/pyodide.tar.bz2",
                "JUPYTERLITE_APP_ARCHIVE": "/tmp/app.tgz",
            }
        )
        self.assertNotIn("JUPYTERLITE_PYODIDE_URL", env)
        self.assertNotIn("JUPYTERLITE_APP_ARCHIVE", env)

    def test_tells_jupyter_to_read_no_config_files(self):
        self.assertEqual(build_wasm.jupyterlite_build_env({})["JUPYTER_NO_CONFIG"], "1")

    def test_keeps_the_rest_of_the_host_environment(self):
        env = build_wasm.jupyterlite_build_env({"PATH": "/usr/bin", "HOME": "/root"})
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/root")


class RunJupyterLiteTestPhaseTest(unittest.TestCase):
    def test_refuses_a_site_whose_kernel_names_another_pyodide(self):
        """The replay runs the cross-build environment's Pyodide in place of
        the one the site names, so a pass on any other site would describe a
        runtime no visitor gets."""
        with tempfile.TemporaryDirectory() as scratch:
            scratch = Path(scratch)
            dist = scratch / "dist"
            dist.mkdir()
            (dist / "pyodide.mjs").write_text("")
            site = scratch / "site"
            site.mkdir()
            (site / "jupyter-lite.json").write_text(json.dumps({
                "jupyter-config-data": {"litePluginSettings": {
                    "@jupyterlite/pyodide-kernel-extension:kernel": {
                        "pyodideUrl": "./static/pyodide/pyodide.js",
                    },
                }},
            }))
            # build_root is patched too, so that a regression in the check
            # reaches scratch files rather than the real build root.
            with mock.patch.object(
                build_wasm, "pyodide_browser_dist_dir", lambda prefix: dist
            ), mock.patch.object(
                build_wasm, "build_root", lambda build_type: scratch
            ), mock.patch.object(build_wasm.subprocess, "run") as run:
                with self.assertRaises(SystemExit) as raised:
                    build_wasm.run_jupyterlite_test_phase(
                        scratch, scratch / "emsdk_env.sh", mock.Mock(type="Release"), site
                    )
            run.assert_not_called()
        self.assertIn("./static/pyodide/pyodide.js", str(raised.exception))

    def run_phase_with_replay_report(self, cells):
        """Run the phase against a scratch site, with a stand-in replay that
        writes ``cells`` as its report. Returns the replay's command line and
        what the phase printed."""
        with tempfile.TemporaryDirectory() as scratch:
            scratch = Path(scratch)
            dist = scratch / "dist"
            dist.mkdir()
            (dist / "pyodide.mjs").write_text("")
            site = scratch / "site"
            site.mkdir()
            (site / "jupyter-lite.json").write_text(
                json.dumps(build_wasm.jupyterlite_config())
            )

            def replay(argv):
                report_file = scratch / "jupyterlite-test-report.json"
                report_file.write_text(json.dumps({"cells": cells}))
                return mock.Mock(returncode=0)

            printed = io.StringIO()
            with mock.patch.object(
                build_wasm, "pyodide_browser_dist_dir", lambda prefix: dist
            ), mock.patch.object(
                build_wasm, "build_root", lambda build_type: scratch
            ), mock.patch.object(
                build_wasm.subprocess, "run", side_effect=replay
            ) as run, contextlib.redirect_stdout(printed):
                build_wasm.run_jupyterlite_test_phase(
                    scratch, scratch / "emsdk_env.sh", mock.Mock(type="Release"), site
                )
        return run.call_args.args[0][2], printed.getvalue()

    def test_tells_the_replay_which_tag_to_skip(self):
        command, _ = self.run_phase_with_replay_report(
            [{"id": "init", "status": "ok", "outputs": [stdout(BANNER)]}]
        )
        self.assertTrue(
            command.endswith(" " + shlex.quote(build_wasm.JUPYTERLITE_BROWSER_ONLY_TAG))
        )

    def test_names_the_cells_the_replay_skipped(self):
        """A skipped cell is code the gate did not run, so a pass says so."""
        _, printed = self.run_phase_with_replay_report(
            [
                {"id": "init", "status": "ok", "outputs": [stdout(BANNER)]},
                {"id": "rcsb", "status": "skipped", "outputs": []},
            ]
        )
        self.assertIn("Notebook test PASSED", printed)
        self.assertIn("Skipped 1 cell(s) tagged browser-only", printed)
        self.assertIn(": rcsb\n", printed)


class ExampleNotebookTest(unittest.TestCase):
    """The notebook the site ships and the notebook test runs."""

    def setUp(self):
        path = Path(__file__).resolve().parent / "wasm_jupyterlite_example.ipynb"
        self.notebook = json.loads(path.read_text(encoding="utf-8"))
        self.code_cells = [
            cell for cell in self.notebook["cells"] if cell["cell_type"] == "code"
        ]

    def test_names_the_kernel_the_site_registers(self):
        """The Pyodide kernel registers itself as `python`. A notebook naming
        any other kernel opens in the browser asking the visitor to pick one."""
        self.assertEqual(self.notebook["metadata"]["kernelspec"]["name"], "python")

    def test_installs_pyrosetta_before_anything_imports_it(self):
        """The site indexes the wheel for `%pip`; nothing installs it on
        import."""
        self.assertEqual("".join(self.code_cells[0]["source"]), "%pip install pyrosetta")

    def test_tags_the_cell_that_fetches_from_rcsb_for_the_browser_alone(self):
        """Its download goes through the browser, which the notebook test's
        Node replay has none of."""
        fetching = [
            cell for cell in self.code_cells if "pose_from_rcsb" in "".join(cell["source"])
        ]
        self.assertEqual(len(fetching), 1)
        self.assertIn(
            build_wasm.JUPYTERLITE_BROWSER_ONLY_TAG, fetching[0]["metadata"]["tags"]
        )

    def test_its_rcsb_cell_downloads_over_https(self):
        """A page served over https cannot fetch a plain http:// URL: Chrome
        blocks it as mixed content. The browser test serves the site over
        http, where Chrome allows it, so only this test sees the URL."""
        rcsb_path = (
            Path(__file__).resolve().parent / "src" / "pyrosetta" / "toolbox" / "rcsb.py"
        )
        spec = importlib.util.spec_from_file_location("rcsb", rcsb_path)
        rcsb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rcsb)
        requested = []

        def urlretrieve(url):
            requested.append(url)
            downloaded = Path(scratch) / "downloaded.pdb"
            downloaded.write_text("ATOM\n" * 40)
            return str(downloaded), None

        with tempfile.TemporaryDirectory() as scratch:
            with mock.patch.object(rcsb, "urllib_urlretrieve", urlretrieve):
                rcsb.load_from_rcsb("1ubq", str(Path(scratch) / "1UBQ.pdb"))
        self.assertEqual(requested, ["https://files.rcsb.org/download/1UBQ.pdb"])

    def test_carries_no_outputs(self):
        """Stored outputs would show a visitor results from whichever build
        last ran the notebook, before they have run anything."""
        for cell in self.code_cells:
            with self.subTest(cell=cell["id"]):
                self.assertEqual(cell["outputs"], [])
                self.assertIsNone(cell["execution_count"])


class JudgeBrowserRunTest(unittest.TestCase):
    """The verdict on a browser run, shared by both browser phases."""

    def setUp(self):
        self.sampler = build_wasm.MemorySampler(os.getpid(), time.monotonic())

    def judge(self, report):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as printed:
            try:
                build_wasm.judge_browser_run(
                    "Some browser test",
                    report,
                    self.sampler,
                    1.0,
                    Path("/nonexistent/chrome.log"),
                )
            except SystemExit as raised:
                return str(raised), printed.getvalue()
        return None, printed.getvalue()

    def test_passes_a_run_that_printed_the_banner(self):
        failure, _ = self.judge({"ok": True, "marks": [], "output": BANNER})
        self.assertIsNone(failure)

    def test_fails_a_run_the_page_reported_as_failed(self):
        failure, _ = self.judge(
            {"ok": False, "error": "ImportError: rosetta.so", "output": BANNER}
        )
        self.assertIn("Some browser test FAILED", failure)
        self.assertIn("ImportError: rosetta.so", failure)

    def test_fails_a_run_whose_output_lacks_the_tracer_lines(self):
        """The box is a Python print, so it can arrive while the C++ tracer's
        lines never do."""
        box = "".join(BANNER.splitlines(keepends=True)[:3])
        failure, _ = self.judge({"ok": True, "output": box})
        self.assertIn("required substrings are absent", failure)
        self.assertIn("core.init:", failure)

    def test_page_text_reaches_the_log_without_its_control_characters(self):
        """Both the output and the error are text the run produced, and either
        can carry a sequence that rewrites the terminal."""
        _, printed = self.judge(
            {"ok": True, "output": BANNER + "\x1b]0;retitled\x07\n"}
        )
        self.assertNotIn("\x1b", printed)
        self.assertIn("\\x1b]0;retitled\\x07\n", printed)
        failure, _ = self.judge({"ok": False, "error": "line 1\n\x1b[2Jline 2"})
        self.assertNotIn("\x1b", failure)
        self.assertIn("line 1\n\\x1b[2Jline 2", failure)

    def test_blanks_a_run_token_in_the_page_error(self):
        """The M1 page reports a failure as a stack trace, and its frames name
        the page by the URL that carries the token."""
        failure, _ = self.judge(
            {
                "ok": False,
                "error": "Error\n    at run (http://127.0.0.1:4/?wheel=w&token=SEKRIT:9)",
            }
        )
        self.assertNotIn("SEKRIT", failure)
        self.assertIn("token=<redacted>", failure)

    def test_blanks_a_run_token_in_the_page_output(self):
        _, printed = self.judge(
            {"ok": True, "output": BANNER + "fetched /?wheel=w&token=SEKRIT\n"}
        )
        self.assertNotIn("SEKRIT", printed)


class OpenPrivateFileTest(unittest.TestCase):
    """The browser's log and launch page carry the run token, and the build
    root is readable by every account on the host."""

    def test_leaves_the_file_readable_by_its_owner_alone(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "chrome.log"
            with build_wasm.open_private_file(path) as handle:
                handle.write("token=SEKRIT")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_replaces_a_readable_file_left_by_an_earlier_run(self):
        """`O_CREAT` keeps an existing file's mode, so the file has to be
        replaced rather than truncated."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "chrome.log"
            path.write_text("the last run's log")
            path.chmod(0o644)
            with build_wasm.open_private_file(path) as handle:
                handle.write("token=SEKRIT")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), "token=SEKRIT")


class JupyterLiteDriveFailureTest(unittest.TestCase):
    """The check that the JupyterLite browser test's kernel mounted /drive."""

    def test_passes_a_kernel_that_started_in_drive(self):
        report = {"marks": [{"name": "kernel-ready", "cwd": "/drive"}]}
        self.assertIsNone(build_wasm.jupyterlite_drive_failure(report))

    def test_fails_a_kernel_that_started_elsewhere(self):
        """Where the worker does not mount the file browser, Pyodide leaves the
        kernel in its home directory."""
        report = {"marks": [{"name": "kernel-ready", "cwd": "/home/pyodide"}]}
        failure = build_wasm.jupyterlite_drive_failure(report)
        self.assertIn("/home/pyodide", failure)
        self.assertIn("did not mount the file browser", failure)

    def test_fails_a_run_that_never_reported_where_its_kernel_started(self):
        report = {"marks": [{"name": "wheel-installed"}]}
        self.assertIsNotNone(build_wasm.jupyterlite_drive_failure(report))


def files_report(round_trip=None, fetched=None):
    """A JupyterLite browser run's report whose file marks passed, with
    ``round_trip`` and ``fetched`` overriding what those marks carry. A mark
    given as None is left out."""
    marks = [
        {"name": "kernel-ready", "cwd": "/drive"},
        {"name": "pdb-round-trip", "cwd": "/drive", "listed": True,
         "identical": True, "residues": 13, "expected": 13, **(round_trip or {})},
        {"name": "rcsb-fetched", "cwd": "/drive", "listed": True, "residues": 76,
         **(fetched or {})},
    ]
    return {"marks": marks}


class JupyterLiteFilesFailureTest(unittest.TestCase):
    """The check that the JupyterLite browser test moved files through /drive."""

    def test_passes_a_run_that_moved_files_both_ways(self):
        self.assertIsNone(build_wasm.jupyterlite_files_failure(files_report()))

    def test_fails_a_run_that_never_reported_the_round_trip(self):
        report = files_report()
        del report["marks"][1]
        failure = build_wasm.jupyterlite_files_failure(report)
        self.assertIn("never reported writing a PDB", failure)

    def test_fails_a_round_trip_made_outside_drive(self):
        """Pyodide's in-memory filesystem passes a round trip without the file
        browser taking part."""
        failure = build_wasm.jupyterlite_files_failure(
            files_report(round_trip={"cwd": "/home/pyodide"})
        )
        self.assertIn("written in /home/pyodide, not /drive", failure)

    def test_fails_when_the_file_browser_does_not_list_the_written_pdb(self):
        failure = build_wasm.jupyterlite_files_failure(
            files_report(round_trip={"listed": False})
        )
        self.assertIn("missing from the file browser's listing", failure)

    def test_fails_when_the_pdb_read_back_differs_from_what_was_written(self):
        failure = build_wasm.jupyterlite_files_failure(
            files_report(round_trip={"identical": False})
        )
        self.assertIn("differs from the bytes Rosetta wrote", failure)

    def test_fails_when_the_reloaded_pose_has_other_residues(self):
        failure = build_wasm.jupyterlite_files_failure(
            files_report(round_trip={"residues": 12})
        )
        self.assertIn("has 12 residues, where the pose written had 13", failure)

    def test_fails_a_run_that_never_reported_fetching_from_rcsb(self):
        report = files_report()
        del report["marks"][2]
        failure = build_wasm.jupyterlite_files_failure(report)
        self.assertIn("never reported fetching a structure from RCSB", failure)

    def test_fails_an_rcsb_fetch_made_outside_drive(self):
        failure = build_wasm.jupyterlite_files_failure(
            files_report(fetched={"cwd": "/home/pyodide"})
        )
        self.assertIn("wrote its PDB in /home/pyodide, not /drive", failure)

    def test_fails_when_the_fetched_pdb_is_not_in_drive(self):
        failure = build_wasm.jupyterlite_files_failure(
            files_report(fetched={"listed": False})
        )
        self.assertIn("missing from /drive", failure)

    def test_fails_when_pose_from_rcsb_loaded_no_residues(self):
        failure = build_wasm.jupyterlite_files_failure(
            files_report(fetched={"residues": 0})
        )
        self.assertIn("loaded 0 residues", failure)


class JupyterLiteBrowserTestServerTest(unittest.TestCase):
    """The server the JupyterLite browser test opens the site from."""

    TOKEN = "the-run-token"

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        site = Path(scratch.name)
        (site / "jupyter-lite.json").write_text('{"jupyter-config-data": {}}')
        self.results = queue.Queue()
        server = build_wasm.JupyterLiteBrowserTestServer(
            site, self.TOKEN, self.results
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def fetch(self, request):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as refused:
            refused.close()
            return refused.code, b""

    def post(self, path, payload):
        return self.fetch(
            urllib.request.Request(
                self.url + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )[0]

    def test_serves_the_site(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            status, body = self.fetch(self.url + "/jupyter-lite.json")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"jupyter-config-data": {}}')

    def test_takes_a_report_carrying_the_run_token(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            status = self.post(
                "/mark", {"token": self.TOKEN, "name": "kernel-ready", "detail": {}}
            )
        self.assertEqual(status, 200)
        self.assertEqual(self.results.get_nowait(), "kernel-ready")

    def test_refuses_a_report_without_the_run_token(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            status = self.post("/result", {"token": "wrong", "ok": True})
        self.assertEqual(status, 403)
        self.assertTrue(self.results.empty())

    def test_logs_a_request_without_its_query_string(self):
        """The REPL's URL carries the cell, and the cell carries the token
        URL-encoded, where `redact_token` would not find it."""
        cell = urllib.parse.urlencode({"code": 'RUN_TOKEN = "SEKRIT"'})
        with mock.patch("sys.stdout", new_callable=io.StringIO) as printed:
            self.fetch(f"{self.url}/jupyter-lite.json?kernel=python&{cell}")
        self.assertNotIn("SEKRIT", printed.getvalue())
        self.assertIn("GET /jupyter-lite.json HTTP", printed.getvalue())


class JupyterLiteBrowserTestCellTest(unittest.TestCase):
    """The REPL cell the JupyterLite browser test sends the site's kernel."""

    def test_carries_the_run_token(self):
        cell = build_wasm.jupyterlite_browser_test_cell("the-run-token")
        self.assertIn('RUN_TOKEN = "the-run-token"\n', cell)
        self.assertNotIn(build_wasm.JUPYTERLITE_BROWSER_TEST_TOKEN_SLOT, cell)

    def test_installs_pyrosetta_the_way_the_example_notebook_does(self):
        """The test stands for a visitor running the notebook, so it installs
        PyRosetta with the notebook's own `%pip` line."""
        notebook = json.loads(
            (
                Path(__file__).resolve().parent / "wasm_jupyterlite_example.ipynb"
            ).read_text(encoding="utf-8")
        )
        install = next(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
        )
        cell = build_wasm.jupyterlite_browser_test_cell("the-run-token")
        self.assertIn(install, [line.strip() for line in cell.splitlines()])

    def test_runs_the_example_notebooks_browser_only_cells_verbatim(self):
        """The notebook test skips these cells, so this is the only test that
        runs their code. A copy that drifted from the notebook would test
        something no visitor runs."""
        notebook = json.loads(
            (
                Path(__file__).resolve().parent / "wasm_jupyterlite_example.ipynb"
            ).read_text(encoding="utf-8")
        )
        browser_only = [
            "".join(cell["source"])
            for cell in notebook["cells"]
            if build_wasm.JUPYTERLITE_BROWSER_ONLY_TAG
            in cell.get("metadata", {}).get("tags", [])
        ]
        self.assertTrue(browser_only)
        cell = build_wasm.jupyterlite_browser_test_cell("the-run-token")
        for source in browser_only:
            with self.subTest(source=source):
                # The test's cell runs everything inside one `try:`.
                self.assertIn(textwrap.indent(source, "    ") + "\n", cell)

    def test_refuses_a_cell_with_nowhere_to_put_the_token(self):
        """Every post from such a cell would be refused, and the run would only
        end at the timeout, 15 minutes later."""
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "wasm_jupyterlite_browser_test.ipy").write_text(
                'RUN_TOKEN = ""\n'
            )
            with mock.patch.object(build_wasm, "script_dir", lambda: Path(scratch)):
                with self.assertRaises(SystemExit):
                    build_wasm.jupyterlite_browser_test_cell("the-run-token")

    def test_refuses_a_cell_with_two_places_for_the_token(self):
        """The cell posts from one place. A second copy of the token would sit
        somewhere else in the cell, such as text it prints into the log."""
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "wasm_jupyterlite_browser_test.ipy").write_text(
                'RUN_TOKEN = "@RUN_TOKEN@"\nOTHER = "@RUN_TOKEN@"\n'
            )
            with mock.patch.object(build_wasm, "script_dir", lambda: Path(scratch)):
                with self.assertRaises(SystemExit):
                    build_wasm.jupyterlite_browser_test_cell("the-run-token")


class ParseArgsTest(unittest.TestCase):
    def test_the_notebook_test_builds_the_site_it_tests(self):
        self.assertTrue(build_wasm.parse_args(["--jupyterlite-test"]).jupyterlite)

    def test_the_jupyterlite_browser_test_builds_the_site_it_tests(self):
        self.assertTrue(
            build_wasm.parse_args(["--jupyterlite-browser-test"]).jupyterlite
        )

    def test_building_the_site_does_not_run_the_notebook_test(self):
        self.assertFalse(build_wasm.parse_args(["--jupyterlite"]).jupyterlite_test)


if __name__ == "__main__":
    unittest.main(verbosity=2)
