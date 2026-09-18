#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# :noTabs=true:

# (c) Copyright Rosetta Commons Member Institutions.
# (c) This file is part of the Rosetta software suite and is made available under license.
# (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
# (c) For more information, see http://www.rosettacommons.org. Questions about this can be
# (c) addressed to University of Washington CoMotion, email: license@uw.edu.

"""Split over-sized linker-generated functions in a WebAssembly side module.

Wasm engines cap the size of a single function body: V8's limit is 7,654,321
bytes, and a module holding one larger function is rejected at compile time
with ``CompileError: size N > maximum function size 7654321`` -- before any
of it runs.

``wasm-ld`` synthesises ``__wasm_apply_data_relocs`` for a ``SIDE_MODULE``:
one store instruction per pointer that lives in the module's data, applied at
load time.  Its size therefore scales with the data section, and for
``rosetta.so`` it lands around 10 MB.  No translation unit is at fault and no
compiler flag shrinks it -- ``-mextended-const`` covers global initialisers,
not pointers embedded in data segments, and leaves this function byte-identical.
Upstream declined to split the function in the linker (llvm-project D140111,
never landed), so it is split here instead.

The transform is a post-link rewrite: the over-sized body becomes a sequence of
calls to new chunk functions appended to the module, each holding a slice of
the original instructions.  New functions take the highest indices, so every
pre-existing function index -- in call sites, element segments, exports and the
start section -- keeps its meaning, and every other section is copied through
byte-for-byte.

Only straight-line code is splittable: a chunk boundary has to be a point where
the operand stack is empty and no local is live across it.  A function that
does not decode that way is reported as an error rather than rewritten, which
makes this script double as the check that the module is loadable at all.

Usage:
    wasm_split_functions.py <module.wasm> [--limit N] [--chunk-size N] [--check]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

# V8's cap on a single function body, from wasm-limits.h.  SpiderMonkey and
# JavaScriptCore are more permissive, so this is the binding constraint.
V8_MAX_FUNCTION_SIZE = 7_654_321

# Chunks are built well under the cap: the accounting below is over the
# instruction bytes, and a chunk also carries the function's locals
# declaration and its own `end`.
DEFAULT_CHUNK_SIZE = 6_000_000


# ---------------------------------------------------------------------------
# LEB128 primitives.
# ---------------------------------------------------------------------------
def read_uleb(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def read_sleb(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            if byte & 0x40:
                result -= 1 << shift
            return result, pos


def write_uleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


# ---------------------------------------------------------------------------
# Module structure.
# ---------------------------------------------------------------------------
@dataclass
class Section:
    """One top-level section: ``payload`` excludes the id and size prefix."""
    id: int
    payload_start: int
    payload_end: int

    @property
    def size(self) -> int:
        return self.payload_end - self.payload_start


def parse_sections(data: bytes) -> list[Section]:
    if data[:4] != b"\0asm":
        raise ValueError("not a WebAssembly module (bad magic)")
    sections = []
    pos = 8
    while pos < len(data):
        section_id = data[pos]
        pos += 1
        size, pos = read_uleb(data, pos)
        sections.append(Section(section_id, pos, pos + size))
        pos += size
    return sections


def count_imported_functions(data: bytes, section: Section) -> int:
    count, pos = read_uleb(data, section.payload_start)
    functions = 0
    for _ in range(count):
        for _field in range(2):  # module name, field name
            length, pos = read_uleb(data, pos)
            pos += length
        kind = data[pos]
        pos += 1
        if kind == 0x00:  # function
            _type, pos = read_uleb(data, pos)
            functions += 1
        elif kind == 0x01:  # table
            pos += 1  # reftype
            limits = data[pos]
            pos += 1
            _min, pos = read_uleb(data, pos)
            if limits & 0x01:
                _max, pos = read_uleb(data, pos)
        elif kind == 0x02:  # memory
            limits = data[pos]
            pos += 1
            _min, pos = read_uleb(data, pos)
            if limits & 0x01:
                _max, pos = read_uleb(data, pos)
        elif kind == 0x03:  # global
            pos += 2  # valtype, mutability
        elif kind == 0x04:  # tag
            pos += 1  # attribute
            _type, pos = read_uleb(data, pos)
        else:
            raise ValueError(f"unhandled import kind {kind:#x}")
    return functions


def parse_function_types(data: bytes, section: Section) -> list[int]:
    """Type index of each function defined in this module, in index order."""
    count, pos = read_uleb(data, section.payload_start)
    types = []
    for _ in range(count):
        type_index, pos = read_uleb(data, pos)
        types.append(type_index)
    return types


def parse_type_signatures(data: bytes, section: Section) -> list[tuple[int, int]]:
    """``(param count, result count)`` per type, enough for call arity."""
    count, pos = read_uleb(data, section.payload_start)
    signatures = []
    for _ in range(count):
        form = data[pos]
        pos += 1
        if form != 0x60:
            raise ValueError(f"unsupported type form {form:#x} (not a function type)")
        params, pos = read_uleb(data, pos)
        pos += params
        results, pos = read_uleb(data, pos)
        pos += results
        signatures.append((params, results))
    return signatures


def parse_code_bodies(data: bytes, section: Section) -> list[tuple[int, int]]:
    """``(start, end)`` of each function body, excluding its size prefix."""
    count, pos = read_uleb(data, section.payload_start)
    bodies = []
    for _ in range(count):
        size, pos = read_uleb(data, pos)
        bodies.append((pos, pos + size))
        pos += size
    return bodies


def parse_function_names(data: bytes, section: Section) -> dict[int, str]:
    """Function names from the ``name`` custom section, when the module has one."""
    length, pos = read_uleb(data, section.payload_start)
    if data[pos:pos + length] != b"name":
        return {}
    pos += length
    names: dict[int, str] = {}
    while pos < section.payload_end:
        subsection = data[pos]
        pos += 1
        size, pos = read_uleb(data, pos)
        end = pos + size
        if subsection == 1:  # function names
            count, cursor = read_uleb(data, pos)
            for _ in range(count):
                index, cursor = read_uleb(data, cursor)
                length, cursor = read_uleb(data, cursor)
                names[index] = data[cursor:cursor + length].decode("utf8", "replace")
                cursor += length
        pos = end
    return names


# ---------------------------------------------------------------------------
# Instruction decoding.
#
# Only what a linker-generated relocation function can contain, plus what
# `wasm-opt` turns it into: constants, global and local access, address
# arithmetic, and stores.  Anything else -- control flow above all -- makes the
# body unsplittable, and is reported rather than guessed at.
# ---------------------------------------------------------------------------
NO_IMMEDIATE = "none"
ULEB_IMMEDIATE = "uleb"
SLEB_IMMEDIATE = "sleb"
MEMARG_IMMEDIATE = "memarg"

# opcode -> (name, immediate kind, pops, pushes)
OPCODES: dict[int, tuple[str, str, int, int]] = {
    0x1A: ("drop", NO_IMMEDIATE, 1, 0),
    0x1B: ("select", NO_IMMEDIATE, 3, 1),
    0x20: ("local.get", ULEB_IMMEDIATE, 0, 1),
    0x21: ("local.set", ULEB_IMMEDIATE, 1, 0),
    0x22: ("local.tee", ULEB_IMMEDIATE, 1, 1),
    0x23: ("global.get", ULEB_IMMEDIATE, 0, 1),
    0x24: ("global.set", ULEB_IMMEDIATE, 1, 0),
    0x41: ("i32.const", SLEB_IMMEDIATE, 0, 1),
    0x42: ("i64.const", SLEB_IMMEDIATE, 0, 1),
}
# Loads: pop an address, push a value.
for _opcode in range(0x28, 0x36):
    OPCODES[_opcode] = (f"load{_opcode:#x}", MEMARG_IMMEDIATE, 1, 1)
# Stores: pop an address and a value.
for _opcode in range(0x36, 0x3F):
    OPCODES[_opcode] = (f"store{_opcode:#x}", MEMARG_IMMEDIATE, 2, 0)
# Integer arithmetic and comparison: binary except for the unary ops below.
for _opcode in list(range(0x45, 0x67)) + list(range(0x67, 0x8B)):
    OPCODES[_opcode] = (f"num{_opcode:#x}", NO_IMMEDIATE, 2, 1)
for _opcode in (0x45, 0x50, 0x67, 0x68, 0x69, 0x79, 0x7A, 0x7B):  # eqz, clz, ctz, popcnt
    OPCODES[_opcode] = (f"unary{_opcode:#x}", NO_IMMEDIATE, 1, 1)
# Conversions (i32.wrap_i64, i64.extend_i32_*, sign extensions).
for _opcode in (0xA7, 0xAC, 0xAD, 0xC0, 0xC1, 0xC2, 0xC3, 0xC4):
    OPCODES[_opcode] = (f"convert{_opcode:#x}", NO_IMMEDIATE, 1, 1)

END_OPCODE = 0x0B
CALL_OPCODE = 0x10


@dataclass
class Instruction:
    start: int          # offset into the body
    end: int
    depth_after: int    # operand stack depth after executing it
    local_read: int | None
    local_write: int | None


class UnsplittableBody(Exception):
    """The body contains something this decoder will not reason about."""


def decode_locals(body: bytes) -> tuple[bytes, int]:
    """Return the locals declaration bytes and the offset where code starts."""
    groups, pos = read_uleb(body, 0)
    for _ in range(groups):
        _count, pos = read_uleb(body, pos)
        pos += 1  # valtype
    return body[:pos], pos


def decode_body(body: bytes, code_start: int, call_arity) -> list[Instruction]:
    """Decode straight-line code, tracking stack depth and local access.

    ``call_arity`` maps a function index to ``(pops, pushes)``.
    """
    instructions: list[Instruction] = []
    pos = code_start
    depth = 0
    end = len(body)
    while pos < end:
        start = pos
        opcode = body[pos]
        pos += 1
        local_read = local_write = None
        if opcode == END_OPCODE:
            if pos != end:
                raise UnsplittableBody(f"`end` at {start} is not the final instruction")
            break
        if opcode == CALL_OPCODE:
            index, pos = read_uleb(body, pos)
            pops, pushes = call_arity(index)
        elif opcode in OPCODES:
            name, immediate, pops, pushes = OPCODES[opcode]
            if immediate == ULEB_IMMEDIATE:
                value, pos = read_uleb(body, pos)
                if name == "local.get":
                    local_read = value
                elif name in ("local.set", "local.tee"):
                    local_write = value
            elif immediate == SLEB_IMMEDIATE:
                _value, pos = read_sleb(body, pos)
            elif immediate == MEMARG_IMMEDIATE:
                align, pos = read_uleb(body, pos)
                if align & 0x40:  # multi-memory: a memory index follows
                    _memory, pos = read_uleb(body, pos)
                _offset, pos = read_uleb(body, pos)
        else:
            raise UnsplittableBody(f"opcode {opcode:#x} at offset {start}")
        depth -= pops
        if depth < 0:
            raise UnsplittableBody(f"operand stack underflow at offset {start}")
        depth += pushes
        instructions.append(Instruction(start, pos, depth, local_read, local_write))
    if depth != 0:
        raise UnsplittableBody(f"body leaves {depth} values on the operand stack")
    return instructions


def split_points(instructions: list[Instruction]) -> list[int]:
    """Indices after which the body may be cut.

    A cut is safe where the operand stack is empty and no local is live across
    it -- every local read after the cut is written after the cut first, so a
    chunk never depends on a value another chunk computed.
    """
    live_in: list[bool] = [False] * (len(instructions) + 1)
    live: set[int] = set()
    for index in range(len(instructions) - 1, -1, -1):
        instruction = instructions[index]
        if instruction.local_write is not None:
            live.discard(instruction.local_write)
        if instruction.local_read is not None:
            live.add(instruction.local_read)
        live_in[index] = bool(live)
    return [i for i in range(len(instructions) - 1)
            if instructions[i].depth_after == 0 and not live_in[i + 1]]


def plan_chunks(instructions: list[Instruction], code_start: int,
                chunk_size: int) -> list[tuple[int, int]]:
    """Byte ranges of the body to move into chunk functions, in order.

    Greedy: each chunk runs to the last safe cut that still fits in
    ``chunk_size``.
    """
    cuts = split_points(instructions)
    if not cuts:
        raise UnsplittableBody("no safe split point (locals or operand stack live throughout)")
    ranges: list[tuple[int, int]] = []
    chunk_start = code_start
    last_fit: int | None = None
    for index in cuts:
        if instructions[index].end - chunk_start <= chunk_size:
            last_fit = index
            continue
        if last_fit is None:
            raise UnsplittableBody(
                f"no split point within {chunk_size:,} bytes of offset {chunk_start}")
        chunk_end = instructions[last_fit].end
        ranges.append((chunk_start, chunk_end))
        chunk_start = chunk_end
        last_fit = index if instructions[index].end - chunk_start <= chunk_size else None
        if last_fit is None:
            raise UnsplittableBody(
                f"no split point within {chunk_size:,} bytes of offset {chunk_start}")
    # What is left after the last cut that overflowed is still open: close it
    # at the last cut that fits, so the tail is only the instructions past that
    # cut rather than a whole chunk plus them.
    tail_end = instructions[-1].end
    if tail_end - chunk_start > chunk_size and last_fit is not None:
        chunk_end = instructions[last_fit].end
        if chunk_end > chunk_start:
            ranges.append((chunk_start, chunk_end))
            chunk_start = chunk_end
    if tail_end - chunk_start > chunk_size:
        raise UnsplittableBody(
            f"no split point within {chunk_size:,} bytes of the end of the body")
    ranges.append((chunk_start, tail_end))
    return ranges


# ---------------------------------------------------------------------------
# Rewriting.
# ---------------------------------------------------------------------------
def build_body(locals_declaration: bytes, code: bytes) -> bytes:
    """A complete function body: size prefix, locals, code, `end`."""
    payload = locals_declaration + code + bytes([END_OPCODE])
    return write_uleb(len(payload)) + payload


def split_module(data: bytes, limit: int, chunk_size: int) -> tuple[bytes, list[str]]:
    """Split every over-sized function body.  Returns the new module and a log."""
    sections = parse_sections(data)
    by_id = {}
    for section in sections:
        by_id.setdefault(section.id, []).append(section)

    if 10 not in by_id:
        raise ValueError("module has no code section")
    code_section = by_id[10][0]
    function_section = by_id[3][0]
    type_section = by_id[1][0]

    imported = count_imported_functions(data, by_id[2][0]) if 2 in by_id else 0
    function_types = parse_function_types(data, function_section)
    signatures = parse_type_signatures(data, type_section)
    bodies = parse_code_bodies(data, code_section)
    names: dict[int, str] = {}
    for section in by_id.get(0, []):
        names.update(parse_function_names(data, section))

    def call_arity(index: int) -> tuple[int, int]:
        if index < imported:
            raise UnsplittableBody(f"call to imported function #{index}")
        return signatures[function_types[index - imported]]

    oversized = [(i, start, end) for i, (start, end) in enumerate(bodies)
                 if end - start > limit]
    log = []
    if not oversized:
        return data, log

    # Chunks are appended after every existing function, so their indices --
    # and therefore the call instructions referring to them -- depend only on
    # how many have been appended so far.
    next_index = imported + len(bodies)
    new_bodies: list[bytes] = []
    new_types: list[int] = []
    replacements: dict[int, bytes] = {}
    for body_index, start, end in oversized:
        function_index = body_index + imported
        name = names.get(function_index, f"#{function_index}")
        type_index = function_types[body_index]
        if signatures[type_index] != (0, 0):
            raise ValueError(
                f"function {name} is over the {limit:,}-byte engine limit and takes "
                f"parameters or returns a result (type #{type_index}); only a "
                f"no-argument function can be split into calls")
        body = data[start:end]
        try:
            locals_declaration, code_start = decode_locals(body)
            instructions = decode_body(body, code_start, call_arity)
            ranges = plan_chunks(instructions, code_start, chunk_size)
        except UnsplittableBody as error:
            raise ValueError(
                f"function {name} is {end - start:,} bytes, over the "
                f"{limit:,}-byte engine limit, and cannot be split: {error}"
            ) from error

        calls = bytearray()
        for chunk_start, chunk_end in ranges:
            chunk = build_body(locals_declaration, body[chunk_start:chunk_end])
            if len(chunk) > limit:
                raise ValueError(f"chunk of {name} is {len(chunk):,} bytes, still over the limit")
            new_bodies.append(chunk)
            new_types.append(type_index)
            calls += bytes([CALL_OPCODE]) + write_uleb(next_index)
            next_index += 1
        replacements[body_index] = build_body(locals_declaration, bytes(calls))
        log.append(f"{name}: {end - start:,} bytes -> {len(ranges)} chunks")

    # Code section: bodies in place, with the over-sized ones replaced, then
    # the chunks appended.
    payload = bytearray()
    payload += write_uleb(len(bodies) + len(new_bodies))
    cursor = code_section.payload_start
    _count, cursor = read_uleb(data, cursor)
    for body_index, (start, end) in enumerate(bodies):
        if body_index in replacements:
            payload += replacements[body_index]
        else:
            payload += data[cursor:end]      # size prefix included
        cursor = end
    for body in new_bodies:
        payload += body
    new_code = bytes(payload)

    # Function section: one type index per chunk, in the order the chunks were
    # appended to the code section, so the two stay aligned.
    payload = bytearray()
    payload += write_uleb(len(function_types) + len(new_types))
    _count, cursor = read_uleb(data, function_section.payload_start)
    payload += data[cursor:function_section.payload_end]
    for type_index in new_types:
        payload += write_uleb(type_index)
    new_function = bytes(payload)

    out = bytearray(data[:8])
    for section in sections:
        if section.id == 10:
            body = new_code
        elif section.id == 3:
            body = new_function
        else:
            body = data[section.payload_start:section.payload_end]
        out += bytes([section.id]) + write_uleb(len(body)) + body
    return bytes(out), log


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def report_oversized(data: bytes, limit: int) -> list[str]:
    sections = parse_sections(data)
    by_id: dict[int, list[Section]] = {}
    for section in sections:
        by_id.setdefault(section.id, []).append(section)
    imported = count_imported_functions(data, by_id[2][0]) if 2 in by_id else 0
    names: dict[int, str] = {}
    for section in by_id.get(0, []):
        names.update(parse_function_names(data, section))
    out = []
    for index, (start, end) in enumerate(parse_code_bodies(data, by_id[10][0])):
        if end - start > limit:
            function_index = index + imported
            out.append(f"{names.get(function_index, f'#{function_index}')}: {end - start:,} bytes")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("module", type=Path, help="wasm module to rewrite in place")
    parser.add_argument("--limit", type=int, default=V8_MAX_FUNCTION_SIZE,
                        help="maximum function body size the engine accepts")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help="target instruction bytes per chunk function")
    parser.add_argument("--check", action="store_true",
                        help="report over-sized functions without rewriting")
    args = parser.parse_args(argv)

    data = args.module.read_bytes()
    if args.check:
        oversized = report_oversized(data, args.limit)
        for entry in oversized:
            print(f"over the {args.limit:,}-byte limit: {entry}")
        print(f"{len(oversized)} function(s) over the limit in {args.module}")
        return 1 if oversized else 0

    new_data, log = split_module(data, args.limit, args.chunk_size)
    if not log:
        print(f"{args.module}: no function over the {args.limit:,}-byte limit")
        return 0
    args.module.write_bytes(new_data)
    for entry in log:
        print(f"split {entry}")
    print(f"{args.module}: {len(data):,} -> {len(new_data):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
