#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# :noTabs=true:

# (c) Copyright Rosetta Commons Member Institutions.
# (c) This file is part of the Rosetta software suite and is made available under license.
# (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
# (c) For more information, see http://www.rosettacommons.org. Questions about this can be
# (c) addressed to University of Washington CoMotion, email: license@uw.edu.

"""Tests for wasm_split_functions.py.

The modules built here are hand-encoded rather than produced by emscripten, so
the suite runs anywhere Python does.  They mimic what ``wasm-ld`` emits for
``__wasm_apply_data_relocs``: a flat run of
``global.get __memory_base; i32.const offset; i32.add; global.get symbol; i32.store``.

Run with:  python3 test_wasm_split_functions.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wasm_split_functions as splitter  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal module builder.
# ---------------------------------------------------------------------------
def section(section_id: int, payload: bytes) -> bytes:
    return bytes([section_id]) + splitter.write_uleb(len(payload)) + payload


def relocation_group(offset: int) -> bytes:
    """One store of a relocated pointer, the shape wasm-ld emits."""
    return (b"\x23\x00"                              # global.get 0 (__memory_base)
            + b"\x41" + splitter.write_uleb(offset)  # i32.const offset
            + b"\x6a"                                # i32.add
            + b"\x23\x01"                            # global.get 1 (symbol address)
            + b"\x36\x02\x00")                       # i32.store align=2 offset=0


def build_module(code: bytes, locals_declaration: bytes = b"\x00",
                 params: int = 0, results: int = 0) -> bytes:
    """A module with two globals and one function holding ``code``."""
    func_type = (b"\x60"
                 + splitter.write_uleb(params) + b"\x7f" * params
                 + splitter.write_uleb(results) + b"\x7f" * results)
    types = section(1, splitter.write_uleb(1) + func_type)
    functions = section(3, splitter.write_uleb(1) + splitter.write_uleb(0))
    globals_ = section(6, splitter.write_uleb(2) + (b"\x7f\x00\x41\x00\x0b") * 2)
    body = locals_declaration + code + b"\x0b"
    code_section = section(10, splitter.write_uleb(1)
                           + splitter.write_uleb(len(body)) + body)
    return b"\0asm\x01\x00\x00\x00" + types + functions + globals_ + code_section


def function_bodies(module: bytes) -> list[bytes]:
    sections = {}
    for each in splitter.parse_sections(module):
        sections.setdefault(each.id, []).append(each)
    return [module[start:end]
            for start, end in splitter.parse_code_bodies(module, sections[10][0])]


def function_type_indices(module: bytes) -> list[int]:
    sections = {}
    for each in splitter.parse_sections(module):
        sections.setdefault(each.id, []).append(each)
    return splitter.parse_function_types(module, sections[3][0])


class SplitsOversizedFunction(unittest.TestCase):
    def setUp(self):
        # 300 groups of 10 bytes; chunked at 100 bytes that is 10 groups a chunk.
        self.code = relocation_group(4) * 300
        self.module = build_module(self.code)

    def test_body_over_the_limit_becomes_calls_to_chunk_functions(self):
        new_module, log = splitter.split_module(self.module, limit=500, chunk_size=100)

        bodies = function_bodies(new_module)
        self.assertEqual(len(bodies), 31, "one rewritten body plus 30 chunks")
        main = bodies[0]
        expected_calls = b"".join(b"\x10" + splitter.write_uleb(index)
                                  for index in range(1, 31))
        self.assertEqual(main, b"\x00" + expected_calls + b"\x0b")
        self.assertEqual(log, ["#0: 3,002 bytes -> 30 chunks"])

    def test_chunks_hold_the_original_instructions_in_order(self):
        new_module, _log = splitter.split_module(self.module, limit=500, chunk_size=100)

        chunks = function_bodies(new_module)[1:]
        rejoined = b"".join(chunk[len(b"\x00"):-len(b"\x0b")] for chunk in chunks)
        self.assertEqual(rejoined, self.code)

    def test_every_chunk_is_under_the_limit(self):
        new_module, _log = splitter.split_module(self.module, limit=500, chunk_size=100)

        for chunk in function_bodies(new_module)[1:]:
            self.assertLessEqual(len(chunk), 500)

    def test_chunks_take_the_type_of_the_function_they_came_from(self):
        new_module, _log = splitter.split_module(self.module, limit=500, chunk_size=100)

        self.assertEqual(function_type_indices(new_module), [0] * 31)

    def test_sections_other_than_code_and_function_are_copied_through(self):
        new_module, _log = splitter.split_module(self.module, limit=500, chunk_size=100)

        original = {s.id: self.module[s.payload_start:s.payload_end]
                    for s in splitter.parse_sections(self.module)}
        rewritten = {s.id: new_module[s.payload_start:s.payload_end]
                     for s in splitter.parse_sections(new_module)}
        self.assertEqual(rewritten[1], original[1])
        self.assertEqual(rewritten[6], original[6])

    def test_locals_declaration_is_repeated_on_every_chunk(self):
        # Declaring the locals on each chunk is what makes local.get/set inside
        # a chunk resolve; a split is only planned where none is live across it.
        declaration = b"\x01\x02\x7f"        # 2 i32 locals
        code = self.code + b"\x41\x00\x21\x00"   # i32.const 0; local.set 0
        module = build_module(code, locals_declaration=declaration)

        new_module, _log = splitter.split_module(module, limit=500, chunk_size=100)

        for body in function_bodies(new_module):
            self.assertTrue(body.startswith(declaration), body[:8].hex())


class LeavesSmallModulesAlone(unittest.TestCase):
    def test_module_with_no_oversized_function_is_returned_unchanged(self):
        module = build_module(relocation_group(4) * 4)

        new_module, log = splitter.split_module(module, limit=500, chunk_size=100)

        self.assertEqual(log, [])
        self.assertIs(new_module, module)

    def test_check_reports_each_oversized_function(self):
        module = build_module(relocation_group(4) * 300)

        self.assertEqual(splitter.report_oversized(module, limit=500),
                         ["#0: 3,002 bytes"])
        self.assertEqual(splitter.report_oversized(module, limit=5000), [])


class RefusesWhatItCannotSplit(unittest.TestCase):
    def test_rejects_control_flow(self):
        code = b"\x02\x40" + relocation_group(4) * 300 + b"\x0b"

        with self.assertRaises(ValueError) as caught:
            splitter.split_module(build_module(code), limit=500, chunk_size=100)

        self.assertIn("opcode 0x2", str(caught.exception))

    def test_rejects_a_function_that_takes_parameters(self):
        code = relocation_group(4) * 300
        module = build_module(code, params=1)

        with self.assertRaises(ValueError) as caught:
            splitter.split_module(module, limit=500, chunk_size=100)

        self.assertIn("takes parameters", str(caught.exception))

    def test_rejects_a_local_that_is_live_across_the_whole_body(self):
        # local 0 is written once at the top and read at the very bottom, so no
        # cut in between is safe.
        code = (b"\x41\x00\x21\x00"                                  # i32.const 0; local.set 0
                + relocation_group(4) * 300
                + b"\x23\x00\x20\x00\x6a\x23\x01\x36\x02\x00")       # ...local.get 0...store
        module = build_module(code, locals_declaration=b"\x01\x01\x7f")

        with self.assertRaises(ValueError) as caught:
            splitter.split_module(module, limit=500, chunk_size=100)

        self.assertIn("cannot be split", str(caught.exception))

    def test_rejects_when_no_cut_fits_the_chunk_size(self):
        code = relocation_group(4) * 300

        with self.assertRaises(ValueError) as caught:
            splitter.split_module(build_module(code), limit=500, chunk_size=5)

        self.assertIn("no split point within", str(caught.exception))


class DecodesRelocationCode(unittest.TestCase):
    def test_split_points_fall_between_complete_stores(self):
        code = relocation_group(4) * 10
        instructions = splitter.decode_body(b"\x00" + code + b"\x0b", 1, lambda i: (0, 0))

        cuts = splitter.split_points(instructions)

        # Five instructions per store, and the cut after the last one is not
        # offered (there is nothing left to put in a second chunk).
        self.assertEqual(cuts, [4, 9, 14, 19, 24, 29, 34, 39, 44])

    def test_operand_stack_must_be_empty_at_a_cut(self):
        instructions = splitter.decode_body(
            b"\x00" + relocation_group(0) + b"\x0b", 1, lambda i: (0, 0))

        depths = [instruction.depth_after for instruction in instructions]

        self.assertEqual(depths, [1, 2, 1, 2, 0])

    def test_unterminated_body_is_reported(self):
        with self.assertRaises(splitter.UnsplittableBody):
            splitter.decode_body(b"\x00\x23\x00\x0b", 1, lambda i: (0, 0))


if __name__ == "__main__":
    unittest.main()
