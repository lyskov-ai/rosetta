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
import tempfile
import unittest
import zipfile
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
