"""DETERMINISTIC EVAL — the Model page reads a model file correctly and safely (spec 046).

A local model is one GGUF file: a header, then tables of numbers, most of them
stored as 4- or 6-bit codes plus a scale. waku/ops/weights.py turns a corner of
a table back into the numbers the model computes with. These tests write a
tiny GGUF file of their own (no model download, no `gguf` package) holding one
table of each type the page decodes, with codes and scales chosen so the right
answer can be worked out by hand, and check three things:

1. the decoders give those hand-worked numbers (F32, F16, Q4_0, Q4_K, Q6_K);
2. a request can only ever open a file Ollama's own manifest points to, inside
   ~/.ollama/models/blobs/ (a name is looked up, never turned into a path);
3. reads are bounded: a corner is at most 64 x 64, and parsing the header does
   not need the tables at all.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from waku.ops import weights

ALIGN = 32


def _s(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def _kv_str(key: str, value: str) -> bytes:
    return _s(key) + struct.pack("<I", 8) + _s(value)


def _kv_u32(key: str, value: int) -> bytes:
    return _s(key) + struct.pack("<II", 4, value)


def _kv_tokens(tokens: list[str]) -> bytes:
    return (_s("tokenizer.ggml.tokens") + struct.pack("<IIQ", 9, 8, len(tokens))
            + b"".join(_s(t) for t in tokens))


# ---- one block of each type, built so the answer is known ----------------------

def q4_0_block() -> bytes:
    # scale 0.5; byte j holds code j (low nibble) and 15 - j (high nibble)
    return struct.pack("<e", 0.5) + bytes(j | ((15 - j) << 4) for j in range(16))


Q4K_SC = [1, 2, 3, 4, 5, 6, 7, 8]
Q4K_MN = [1, 1, 1, 1, 2, 2, 2, 2]


def q4_k_block() -> bytes:
    # d = 1.0, dmin = 0.5; sub-block scales Q4K_SC, mins Q4K_MN (all < 16, so
    # the packed high bits are zero); every byte holds code 1 low, code 3 high
    s = bytes(Q4K_SC[:4]) + bytes(Q4K_MN[:4]) + bytes(
        (Q4K_SC[j + 4] & 0x0F) | ((Q4K_MN[j + 4] & 0x0F) << 4) for j in range(4))
    return struct.pack("<ee", 1.0, 0.5) + s + bytes([0x31]) * 128


def q4_k_expected() -> tuple[list[float], list[int]]:
    values, codes = [], []
    for sub in range(8):
        code = 1 if sub % 2 == 0 else 3
        for _ in range(32):
            codes.append(code)
            values.append(Q4K_SC[sub] * code - 0.5 * Q4K_MN[sub])
    return values, codes


def q6_k_block() -> bytes:
    # low nibbles 1 / high nibbles 2; the two high bits of group k are k;
    # every scale 2; d = 0.25
    return bytes([0x21]) * 128 + bytes([0xE4]) * 64 + struct.pack("<16b", *[2] * 16) + struct.pack("<e", 0.25)


def q6_k_expected() -> tuple[list[float], list[int]]:
    values, codes = [], []
    for g in range(8):
        k = g % 4
        code = (1 if k < 2 else 2) | (k << 4)
        for _ in range(32):
            codes.append(code)
            values.append(0.25 * 2 * (code - 32))
    return values, codes


TABLES = [
    # name, dims innermost-first, ggml type id, data
    ("a.f32", [4], 0, struct.pack("<4f", 1.0, -2.5, 0.0, 3.25)),
    ("b.f16", [4], 1, struct.pack("<4e", 0.5, -1.0, 2.0, -0.25)),
    ("c.q4_0", [32], 2, q4_0_block()),
    ("d.q4_k", [256], 12, q4_k_block()),
    ("e.q6_k", [256], 14, q6_k_block()),
    # 3 rows x 4 columns, value = row * 10 + column
    ("f.grid", [4, 3], 0, struct.pack("<12f", *[r * 10 + c for r in range(3) for c in range(4)])),
    ("g.q8_0", [32], 8, bytes(34)),
]


def write_gguf(path: Path, *, truncate_data: bool = False) -> int:
    """Write the fixture. Returns where the header ends."""
    kv = [_kv_str("general.architecture", "tiny"), _kv_u32("tiny.block_count", 2),
          _kv_u32("general.alignment", ALIGN), _kv_tokens(["<s>", "Ġthe", "cat"])]
    infos, data, offset = b"", b"", 0
    for name, dims, gtype, raw in TABLES:
        infos += _s(name) + struct.pack("<I", len(dims)) + struct.pack(f"<{len(dims)}Q", *dims)
        infos += struct.pack("<IQ", gtype, offset)
        pad = (-len(raw)) % ALIGN
        data += raw + bytes(pad)
        offset += len(raw) + pad
    header = b"GGUF" + struct.pack("<IQQ", 3, len(TABLES), len(kv)) + b"".join(kv) + infos
    header_end = len(header)
    header += bytes((-len(header)) % ALIGN)
    path.write_bytes(header + (b"" if truncate_data else data))
    return header_end


def install(root: Path, name: str = "tiny", tag: str = "latest", **kw) -> Path:
    """Lay the fixture out the way Ollama does: a manifest naming a blob by digest."""
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    digest = "ab" * 32
    blob = blobs / f"sha256-{digest}"
    write_gguf(blob, **kw)
    man = root / "manifests" / "registry.ollama.ai" / "library" / name / tag
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"layers": [
        {"mediaType": "application/vnd.ollama.image.model", "digest": f"sha256:{digest}"}]}))
    return blob


@pytest.fixture
def model(tmp_path):
    install(tmp_path)
    return weights.get_model("tiny:latest", tmp_path)


# ---- 1. the numbers ------------------------------------------------------------------

def test_the_header_lists_every_table_rows_first(model):
    s = model.summary()
    assert [t["name"] for t in s["tensors"]] == [t[0] for t in TABLES]
    assert s["metadata"]["block_count"] == 2
    assert s["metadata"]["vocab_size"] == 3
    grid = model.by_name["f.grid"]
    assert grid["shape"] == [3, 4] and grid["count"] == 12


def test_plain_floats_are_shown_exactly(model):
    assert model.corner("a.f32", 1, 4)["values"] == [[1.0, -2.5, 0.0, 3.25]]
    assert model.corner("b.f16", 1, 4)["values"] == [[0.5, -1.0, 2.0, -0.25]]


def test_a_two_dimensional_corner_reads_row_by_row(model):
    c = model.corner("f.grid", 2, 3)
    assert c["values"] == [[0, 1, 2], [10, 11, 12]]
    assert (c["total_rows"], c["total_cols"]) == (3, 4)


def test_q4_0_is_scale_times_code_minus_eight(model):
    c = model.corner("c.q4_0", 1, 32)
    codes = list(range(16)) + list(range(15, -1, -1))
    assert c["codes"] == [codes]
    assert c["values"] == [[0.5 * (q - 8) for q in codes]]
    assert c["code_levels"] == 16


def test_q4_k_is_scale_times_code_minus_offset(model):
    values, codes = q4_k_expected()
    c = model.corner("d.q4_k", 1, 64)
    assert c["codes"] == [codes[:64]]
    assert c["values"] == [values[:64]]
    full = weights.dec_q4_k(q4_k_block())
    assert full == (values, codes)


def test_q6_k_is_scale_times_code_minus_thirty_two(model):
    values, codes = q6_k_expected()
    assert weights.dec_q6_k(q6_k_block()) == (values, codes)
    c = model.corner("e.q6_k", 1, 64)
    assert c["values"] == [values[:64]] and c["code_levels"] == 64


def test_a_type_it_cannot_decode_shows_no_numbers(model):
    c = model.corner("g.q8_0", 4, 4)
    assert c["values"] is None and c["codes"] is None
    assert "does not decode" in c["note"]


def test_the_vocabulary_is_read_from_the_header(model):
    t = model.token_slice(1, 5)
    assert t["total"] == 3 and [x["text"] for x in t["tokens"]] == ["Ġthe", "cat"]


# ---- 2. only Ollama's own files --------------------------------------------------

def test_a_model_is_found_through_its_manifest(tmp_path):
    install(tmp_path)
    found = weights.list_models(tmp_path)
    assert [m["name"] for m in found] == ["tiny:latest"]
    assert "_blob" not in weights.public_models(tmp_path)[0]


@pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "tiny", "blobs/sha256-" + "ab" * 32])
def test_a_name_is_looked_up_never_opened(tmp_path, name):
    install(tmp_path)
    with pytest.raises(KeyError):
        weights.get_model(name, tmp_path)


def test_a_manifest_cannot_point_outside_the_blobs_folder(tmp_path):
    install(tmp_path)
    outside = tmp_path / "secret.gguf"
    write_gguf(outside)
    bad = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "evil" / "latest"
    bad.parent.mkdir(parents=True)
    bad.write_text(json.dumps({"layers": [
        {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:../../secret.gguf"}]}))
    link_digest = "cd" * 32
    (tmp_path / "blobs" / f"sha256-{link_digest}").symlink_to(outside)
    linked = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "linked" / "latest"
    linked.parent.mkdir(parents=True)
    linked.write_text(json.dumps({"layers": [
        {"mediaType": "application/vnd.ollama.image.model", "digest": f"sha256:{link_digest}"}]}))
    assert [m["name"] for m in weights.list_models(tmp_path)] == ["tiny:latest"]


def test_the_folder_is_ollamas_and_no_setting_moves_it(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OLLAMA_MODELS", "/etc")
    assert weights.models_root() == tmp_path / ".ollama" / "models"


def test_load_refuses_a_model_that_is_not_installed(monkeypatch, tmp_path):
    install(tmp_path)
    monkeypatch.setattr(weights, "models_root", lambda: tmp_path)

    def no_call(*a, **k):
        raise AssertionError("Ollama must not be called for an unknown model")

    monkeypatch.setattr(weights, "ollama_json", no_call)
    status, body = weights.answer_post("load", {"model": "../../x"})
    assert status == 404 and "not installed" in body["error"]
    assert weights.answer_post("delete", {"model": "tiny:latest"})[0] == 404


def test_load_and_unload_use_ollamas_keep_alive():
    assert weights.keep_alive_request("tiny:latest", True)["keep_alive"] == "5m"
    assert weights.keep_alive_request("tiny:latest", False)["keep_alive"] == 0
    run = weights.run_request("tiny:latest", "hi")
    assert run["logprobs"] is True and run["top_logprobs"] == 5 and run["stream"] is True


def test_ollama_is_only_ever_this_machine():
    assert (weights.OLLAMA_HOST, weights.OLLAMA_PORT) == ("127.0.0.1", 11434)


# ---- 3. bounded reads ------------------------------------------------------------

def test_a_corner_larger_than_64_is_refused(model):
    with pytest.raises(ValueError):
        model.corner("d.q4_k", 65, 1)
    with pytest.raises(ValueError):
        model.corner("d.q4_k", 1, 65)
    assert weights.answer_get("tensor", {"name": "x", "rows": "65"})[0] in (400, 404)


def test_the_header_parses_without_the_tables(tmp_path):
    install(tmp_path, truncate_data=True)
    m = weights.get_model("tiny:latest", tmp_path)
    assert len(m.summary()["tensors"]) == len(TABLES)
    assert m.header_end <= m.file_size
    with pytest.raises(ValueError):
        m.corner("a.f32", 1, 4)
