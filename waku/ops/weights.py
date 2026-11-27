"""How a model works: read the file Ollama keeps, and watch one run (spec 046).

A local model is one file of numbers. Ollama keeps it under
``~/.ollama/models/blobs/`` in the GGUF format: a header (metadata, the
vocabulary, a list of tables) followed by the tables themselves, most of them
quantised to 4 or 6 bits per number. This module is what the dashboard's Model
page reads, and it does four things:

1. **Find the models** through Ollama's own manifests. A request names a model
   ("qwen3:8b"), never a path; the name is looked up among the manifests, and
   the blob it points to must sit inside the blobs folder. That is the whole
   defence a local file-reading route needs (spec 046, decision 2).
2. **Parse the header** and list every table, without reading the tables.
3. **Decode a corner** of one table, at most 64 x 64, with the same arithmetic
   as llama.cpp, so the numbers shown are the numbers the model computes with
   (decision 3). F32, F16, Q4_0, Q4_K and Q6_K; any other type shows a notice
   instead of invented numbers.
4. **Relay Ollama on this machine** (127.0.0.1:11434, never another host): one
   streamed generation with the top 5 candidates per token, and what is loaded
   in memory. A run loads the model and Ollama unloads it by itself.

Ported from the open-models video's standalone viewer, where the decoders were
cross-checked bit for bit against ``gguf.quants.dequantize`` (decision 4: a
copy, because ``waku/`` imports nothing from outside the package). Standard
library only. Nothing here writes to ``~/.ollama``.
"""

from __future__ import annotations

import http.client
import json
import math
import mmap
import re
import struct
import subprocess
import threading
from pathlib import Path

OLLAMA_HOST, OLLAMA_PORT = "127.0.0.1", 11434
MAX_CORNER = 64
MAX_TOKENS = 500

# ggml tensor type id -> (name, weights per block, bytes per block)
GGML_TYPES = {
    0: ("F32", 1, 4), 1: ("F16", 1, 2),
    2: ("Q4_0", 32, 18), 3: ("Q4_1", 32, 20),
    6: ("Q5_0", 32, 22), 7: ("Q5_1", 32, 24),
    8: ("Q8_0", 32, 34), 9: ("Q8_1", 32, 36),
    10: ("Q2_K", 256, 84), 11: ("Q3_K", 256, 110), 12: ("Q4_K", 256, 144),
    13: ("Q5_K", 256, 176), 14: ("Q6_K", 256, 210), 15: ("Q8_K", 256, 292),
    24: ("I8", 1, 1), 25: ("I16", 1, 2), 26: ("I32", 1, 4), 27: ("I64", 1, 8),
    28: ("F64", 1, 8), 30: ("BF16", 1, 2),
}
_BY_NAME = {v[0]: v for v in GGML_TYPES.values()}
DECODABLE = {"F32", "F16", "Q4_0", "Q4_K", "Q6_K"}

FILE_TYPES = {
    0: "ALL_F32", 1: "MOSTLY_F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0",
    9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
    14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
    32: "BF16",
}

_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
            7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_VOCAB_KEY = "tokenizer.ggml.tokens"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def models_root() -> Path:
    """Ollama's own folder. Fixed on purpose: a WAKU_* setting pointing it
    elsewhere would turn these routes into a reader of any folder."""
    return Path.home() / ".ollama" / "models"


def _tilde(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home) else text


# ---------------------------------------------------------------- Ollama store

def list_models(root: Path | None = None) -> list[dict]:
    """Every installed model, read from Ollama's manifests.

    Each manifest names its weights layer by digest. A digest that is not
    ``sha256:<64 hex>``, or a blob that resolves outside ``blobs/``, is
    skipped: the manifest is a file on disk, and a file on disk is not trusted
    to say where to read."""
    root = root or models_root()
    manifests, blobs = root / "manifests", (root / "blobs").resolve()
    out: list[dict] = []
    if not manifests.is_dir():
        return out
    for path in sorted(manifests.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        parts = path.relative_to(manifests).parts
        if len(parts) < 4:
            continue
        namespace, name, tag = parts[-3], parts[-2], parts[-1]
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for layer in manifest.get("layers", []) if isinstance(manifest, dict) else []:
            if not str(layer.get("mediaType", "")).endswith(".model"):
                continue
            digest = str(layer.get("digest", ""))
            if not _DIGEST.fullmatch(digest):
                break
            blob = (blobs / digest.replace(":", "-")).resolve()
            if blob.parent != blobs or not blob.is_file():
                break
            full = f"{name}:{tag}" if namespace == "library" else f"{namespace}/{name}:{tag}"
            out.append({"name": full, "path": _tilde(blob), "size": blob.stat().st_size,
                        "_blob": blob})
            break
    out.sort(key=lambda m: m["name"])
    return out


# ---------------------------------------------------------------- GGUF header

class Model:
    """One GGUF file, opened read-only and memory-mapped.

    The map costs nothing until a page is touched, so parsing the header reads
    only the header (a few MB, most of it the vocabulary), and decoding a
    corner reads only the blocks that corner sits in."""

    def __init__(self, name: str, blob: Path):
        self.name = name
        self.blob = blob
        self.file_size = blob.stat().st_size
        with open(blob, "rb") as f:
            self.mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        self.meta: dict = {}
        self.tokens: list = []
        self.tensors: list[dict] = []
        self.by_name: dict[str, dict] = {}
        self.header_end = 0
        self._parse()

    def _parse(self) -> None:
        mm = self.mm
        u32 = struct.Struct("<I").unpack_from
        u64 = struct.Struct("<Q").unpack_from
        if mm[:4] != b"GGUF":
            raise ValueError("not a GGUF file")
        self.version = u32(mm, 4)[0]
        n_tensors = u64(mm, 8)[0]
        n_kv = u64(mm, 16)[0]
        pos = 24

        def read_str(p):
            n = u64(mm, p)[0]
            p += 8
            return mm[p:p + n].decode("utf-8", "replace"), p + n

        def read_val(t, p, key):
            if t in _SCALARS:
                s = struct.Struct(_SCALARS[t])
                return s.unpack_from(mm, p)[0], p + s.size
            if t == 8:
                return read_str(p)
            if t == 9:
                et = u32(mm, p)[0]
                cnt = u64(mm, p + 4)[0]
                p += 12
                keep = key == _VOCAB_KEY or cnt <= 64
                if et in _SCALARS and not keep:
                    return {"array_len": cnt}, p + cnt * struct.calcsize(_SCALARS[et])
                if et == 8 and not keep:
                    for _ in range(cnt):
                        p += 8 + u64(mm, p)[0]
                    return {"array_len": cnt}, p
                items = []
                for _ in range(cnt):
                    v, p = read_val(et, p, None)
                    items.append(v)
                return items, p
            raise ValueError(f"unknown metadata type {t}")

        for _ in range(n_kv):
            key, pos = read_str(pos)
            t = u32(mm, pos)[0]
            val, pos = read_val(t, pos + 4, key)
            if key == _VOCAB_KEY:
                self.tokens = val
                val = {"array_len": len(val)}
            self.meta[key] = val

        for _ in range(n_tensors):
            name, pos = read_str(pos)
            nd = u32(mm, pos)[0]
            pos += 4
            dims = list(struct.unpack_from(f"<{nd}Q", mm, pos))
            pos += 8 * nd
            gt = u32(mm, pos)[0]
            off = u64(mm, pos + 4)[0]
            pos += 12
            count = 1
            for d in dims:
                count *= d
            tname, per_block, block_bytes = GGML_TYPES.get(gt, (f"type_{gt}", 0, 0))
            t = {
                "name": name,
                # GGUF stores dims innermost-first; show them rows-first like numpy.
                "shape": dims[::-1],
                "type": tname,
                "count": count,
                "bytes": (count // per_block) * block_bytes if per_block else None,
                "_offset": off,
            }
            self.tensors.append(t)
            self.by_name[name] = t

        self.header_end = pos
        align = int(self.meta.get("general.alignment", 32) or 32)
        self.data_base = (pos + align - 1) // align * align

    def summary(self) -> dict:
        m = self.meta
        arch = m.get("general.architecture", "")

        def g(suffix):
            return m.get(f"{arch}.{suffix}")

        ft = m.get("general.file_type")
        total = sum(t["count"] for t in self.tensors)
        type_counts: dict[str, int] = {}
        for t in self.tensors:
            type_counts[t["type"]] = type_counts.get(t["type"], 0) + 1
        lic = m.get("general.license")
        if isinstance(lic, str) and len(lic) > 200:
            lic = lic[:200] + "..."
        return {
            "name": self.name,
            "path": _tilde(self.blob),
            "gguf_version": self.version,
            "metadata_keys": len(m),
            "metadata": {
                "architecture": arch,
                "model_name": m.get("general.name") or m.get("general.basename"),
                "block_count": g("block_count"),
                "context_length": g("context_length"),
                "embedding_length": g("embedding_length"),
                "vocab_size": len(self.tokens) or g("vocab_size"),
                "license": lic,
                "file_type": ft,
                "quantization": FILE_TYPES.get(ft, None if ft is None else f"file_type {ft}"),
            },
            "totals": {
                "tensor_count": len(self.tensors),
                "total_weights": total,
                "file_size": self.file_size,
                "bits_per_weight": self.file_size * 8 / total if total else None,
                "type_counts": type_counts,
            },
            "tensors": [{k: v for k, v in t.items() if not k.startswith("_")}
                        for t in self.tensors],
        }

    def token_slice(self, start: int, count: int) -> dict:
        n = len(self.tokens)
        start = max(0, min(start, max(n - 1, 0)))
        count = max(1, min(count, MAX_TOKENS))
        return {"name": self.name, "total": n, "start": start,
                "tokens": [{"id": i, "text": self.tokens[i]}
                           for i in range(start, min(n, start + count))]}

    def corner(self, tname: str, rows: int, cols: int) -> dict:
        """The top-left rows x cols of one table, decoded. Larger than 64 on
        either side is refused, not clipped, so a caller cannot mistake a
        clipped answer for the one it asked for."""
        if rows > MAX_CORNER or cols > MAX_CORNER:
            raise ValueError(f"a corner is at most {MAX_CORNER} x {MAX_CORNER}")
        t = self.by_name.get(tname)
        if t is None:
            raise KeyError(f"no table named {tname!r}")
        shape = t["shape"]
        if len(shape) == 0:
            total_rows, total_cols = 1, 1
        elif len(shape) == 1:
            total_rows, total_cols = 1, shape[0]
        else:
            # 2-D: rows x cols. More dims: the first slice of the leading dims.
            total_rows, total_cols = shape[-2], shape[-1]
        rows = max(1, min(rows, total_rows))
        cols = max(1, min(cols, total_cols))
        res = {
            "model": self.name, "name": tname, "shape": shape, "type": t["type"],
            "count": t["count"], "bytes": t["bytes"],
            "total_rows": total_rows, "total_cols": total_cols,
            "rows": rows, "cols": cols,
            "values": None, "codes": None, "code_levels": None,
            "exact": False, "note": "",
        }
        ty = t["type"]
        if ty not in DECODABLE:
            res["note"] = (f"This view does not decode type {ty}, so no numbers "
                           "are shown rather than invented ones.")
            return res
        base = self.data_base + t["_offset"]
        if base + (t["bytes"] or 0) > self.file_size:
            raise ValueError("the table runs past the end of the file")
        _n, per_block, block_bytes = _BY_NAME[ty]
        decode = {"F32": dec_f32, "F16": dec_f16, "Q4_0": dec_q4_0,
                  "Q4_K": dec_q4_k, "Q6_K": dec_q6_k}[ty]
        cache: dict[int, tuple] = {}
        values, codes = [], []
        has_codes = False
        for r in range(rows):
            vrow, crow = [], []
            for c in range(cols):
                flat = r * total_cols + c
                b = flat // per_block
                if b not in cache:
                    o = base + b * block_bytes
                    cache[b] = decode(self.mm[o:o + block_bytes])
                vals, cds = cache[b]
                v = vals[flat % per_block]
                vrow.append(v if math.isfinite(v) else None)   # JSON has no NaN or infinity
                if cds is not None:
                    has_codes = True
                    crow.append(cds[flat % per_block])
            values.append(vrow)
            codes.append(crow)
        res["values"] = values
        res["exact"] = True
        if has_codes:
            res["codes"] = codes
            res["code_levels"] = 64 if ty == "Q6_K" else 16
            res["note"] = {
                "Q4_K": "Stored as 4-bit codes (16 levels) plus a scale and an offset per group of 32; the numbers shown are the decoded values llama.cpp computes with.",
                "Q6_K": "Stored as 6-bit codes (64 levels) plus a scale per group of 16; the numbers shown are the decoded values llama.cpp computes with.",
                "Q4_0": "Stored as 4-bit codes (16 levels) plus one scale per group of 32; the numbers shown are the decoded values llama.cpp computes with.",
            }[ty]
        else:
            bits = "32" if ty == "F32" else "16"
            res["note"] = f"Stored as plain {bits}-bit floating point numbers; shown exactly as in the file."
        return res


# ---------------------------------------------------------------- decoders
# Each takes one block's bytes and returns (values, codes). They follow
# llama.cpp and gguf-py's quants.py, float32 rounding included.

def _f32(x: float) -> float:
    """Round a Python double to float32, as the reference does its maths in float32."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def dec_f32(b: bytes):
    return [struct.unpack("<f", b)[0]], None


def dec_f16(b: bytes):
    return [struct.unpack("<e", b)[0]], None


def dec_q4_0(b: bytes):
    d = struct.unpack_from("<e", b, 0)[0]
    qs = b[2:18]
    codes = [x & 0x0F for x in qs] + [x >> 4 for x in qs]
    return [_f32(d * (q - 8)) for q in codes], codes


def dec_q4_k(b: bytes):
    d, dmin = struct.unpack_from("<ee", b, 0)
    s = b[4:16]
    qs = b[16:144]
    sc, mn = [0] * 8, [0] * 8
    for j in range(4):
        sc[j] = s[j] & 0x3F
        mn[j] = s[j + 4] & 0x3F
        sc[j + 4] = (s[j + 8] & 0x0F) | ((s[j] >> 2) & 0x30)
        mn[j + 4] = (s[j + 8] >> 4) | ((s[j + 4] >> 2) & 0x30)
    values, codes = [], []
    for sub in range(8):
        chunk = qs[(sub // 2) * 32:(sub // 2) * 32 + 32]
        shift = 4 * (sub % 2)
        dd = _f32(d * sc[sub])
        dm = _f32(dmin * mn[sub])
        for x in chunk:
            q = (x >> shift) & 0x0F
            codes.append(q)
            values.append(_f32(_f32(dd * q) - dm))
    return values, codes


def dec_q6_k(b: bytes):
    ql = b[0:128]
    qh = b[128:192]
    scales = struct.unpack_from("<16b", b, 192)
    d = struct.unpack_from("<e", b, 208)[0]
    values, codes = [], []
    for g in range(8):           # 8 groups of 32 weights
        c, k = g // 4, g % 4
        lo = ql[c * 64 + (k % 2) * 32:c * 64 + (k % 2) * 32 + 32]
        hi = qh[c * 32:c * 32 + 32]
        lshift, hshift = 4 * (k // 2), 2 * k
        for i in range(32):
            code = ((lo[i] >> lshift) & 0x0F) | (((hi[i] >> hshift) & 3) << 4)
            codes.append(code)
            scale = _f32(d * scales[(g * 32 + i) // 16])
            values.append(_f32(scale * (code - 32)))
    return values, codes


# ---------------------------------------------------------------- open files

_open: dict[tuple[str, str], Model] = {}
_lock = threading.Lock()


def get_model(name: str, root: Path | None = None) -> Model:
    """The parsed file for an installed model name. A name that is not in
    Ollama's manifests is a KeyError: no path is ever built from it."""
    for m in list_models(root):
        if m["name"] == name:
            key = (name, str(m["_blob"]))
            with _lock:
                if key not in _open:
                    _open[key] = Model(name, m["_blob"])
                return _open[key]
    raise KeyError(f"model {name!r} is not installed in Ollama")


# ---------------------------------------------------------------- Ollama on this machine

def ollama(method: str, path: str, body: dict | None = None, timeout: float = 600):
    """Open a request to the Ollama on this machine. Returns (connection, response)."""
    conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=timeout)
    data = None if body is None else json.dumps(body).encode("utf-8")
    conn.request(method, path, body=data,
                 headers={"Content-Type": "application/json"} if data else {})
    return conn, conn.getresponse()


def ollama_json(method: str, path: str, body: dict | None = None, timeout: float = 600):
    conn, resp = ollama(method, path, body, timeout)
    try:
        raw = resp.read()
    finally:
        conn.close()
    try:
        obj = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except ValueError:
        obj = {"error": raw.decode("utf-8", "replace")[:300]}
    return resp.status, obj


def run_request(model: str, prompt: str) -> dict:
    """The body of one streamed generation: no thinking, and the top 5
    candidates for every piece (Ollama 0.40 and later)."""
    return {"model": model, "prompt": prompt, "stream": True, "think": False,
            "logprobs": True, "top_logprobs": 5, "options": {"temperature": 0.6}}


def _sysctl(key: str) -> str | None:
    try:
        return subprocess.run(["/usr/sbin/sysctl", "-n", key], capture_output=True, check=False,
                              text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


_machine: dict | None = None


def machine() -> dict:
    """Chip name and total memory, asked of the operating system once."""
    global _machine
    if _machine is None:
        mem = _sysctl("hw.memsize")
        _machine = {"chip": _sysctl("machdep.cpu.brand_string"),
                    "memory_bytes": int(mem) if mem and mem.isdigit() else None}
    out = dict(_machine)
    try:
        _st, v = ollama_json("GET", "/api/version", timeout=3)
        out["ollama_version"] = v.get("version")
    except OSError:
        out["ollama_version"] = None
    return out


def public_models(root: Path | None = None) -> list[dict]:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in list_models(root)]


# ---------------------------------------------------------------- the routes' answers
# dashboard.py serves these under /api/weights/: a GET names what to read in
# its query (?what=tensor&name=...) and gets (status, body) back. The one
# POST is the streamed run, which needs the socket itself; it is in
# dashboard.py, using run_request() and ollama() above.

def _int(q: dict, key: str, default: int) -> int:
    try:
        return int(q.get(key, default))
    except (TypeError, ValueError):
        return default


def answer_get(route: str, q: dict) -> tuple[int, dict]:
    """GET /api/weights/read?what=<route>. Every one of them only reads."""
    try:
        if route == "models":
            return 200, {"models": public_models()}
        if route == "model":
            return 200, get_model(q.get("name", "")).summary()
        if route == "tensor":
            return 200, get_model(q.get("name", "")).corner(
                q.get("tensor", ""), _int(q, "rows", 16), _int(q, "cols", 16))
        if route == "tokens":
            return 200, get_model(q.get("name", "")).token_slice(
                _int(q, "start", 0), _int(q, "count", 40))
        if route == "ps":
            try:
                _st, obj = ollama_json("GET", "/api/ps", timeout=5)
            except OSError as e:
                return 502, {"error": f"Ollama is not reachable: {e}"}
            return 200, obj
        if route == "machine":
            return 200, machine()
        return 404, {"error": "not found"}
    except KeyError as e:
        return 404, {"error": str(e.args[0])}
    except ValueError as e:
        return 400, {"error": str(e)}
