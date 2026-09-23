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
import csv
import hashlib
import importlib.util
import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
