"""Tests for captioner: model discovery, prompt store, image encoding, and the
LlamaServer process lifecycle against a stand-in server script (no GPU)."""
from __future__ import annotations

import base64
import io
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest

from PIL import Image

import captioner

# Minimal llama-server stand-in: honours --port, answers /health and
# /v1/chat/completions (echoing the prompt and the image's decoded size).
# FAKE_SERVER_MODE=crash exits immediately to simulate a failed load.
_FAKE_SERVER = textwrap.dedent(f"""\
    #!{sys.executable}
    import base64, io, json, os, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    if os.environ.get("FAKE_SERVER_MODE") == "crash":
        print("fake: model load failed", flush=True)
        sys.exit(3)
    port = int(sys.argv[sys.argv.index("--port") + 1])

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            self._send(200 if self.path == "/health" else 404, {{"status": "ok"}})
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            content = req["messages"][0]["content"]
            url = content[0]["image_url"]["url"]
            from PIL import Image
            img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
            text = f"{{content[1]['text']}}|{{img.width}}x{{img.height}}"
            self._send(200, {{"choices": [{{"message": {{"content": "  " + text + "  "}}}}]}})

    HTTPServer(("127.0.0.1", port), H).serve_forever()
""")


def _write_fake_server(dirpath: str) -> str:
    path = os.path.join(dirpath, "llama-server")
    with open(path, "w") as f:
        f.write(_FAKE_SERVER)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()


class DiscoverModelsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_missing_models_dir_is_empty(self):
        self.assertEqual(captioner.discover_models(self.tmp), ([], []))

    def test_discovers_model_mmproj_and_ini(self):
        d = os.path.join(self.tmp, "models", "Alpha")
        _touch(os.path.join(d, "alpha.Q8_0.gguf"))
        _touch(os.path.join(d, "alpha.mmproj-f16.gguf"))
        with open(os.path.join(d, "model.ini"), "w") as f:
            f.write("[model]\nmax_pixels = 640000\n")
        _touch(os.path.join(self.tmp, "models", "Beta", "b.gguf"))
        _touch(os.path.join(self.tmp, "models", "Beta", "mmproj-b.gguf"))
        os.makedirs(os.path.join(self.tmp, "models", "Empty"))

        specs, problems = captioner.discover_models(self.tmp)
        self.assertEqual(problems, [])
        self.assertEqual([s.name for s in specs], ["Alpha", "Beta"])
        self.assertTrue(specs[0].model_path.endswith("alpha.Q8_0.gguf"))
        self.assertTrue(specs[0].mmproj_path.endswith("alpha.mmproj-f16.gguf"))
        self.assertEqual(specs[0].max_pixels, 640000)
        self.assertIsNone(specs[1].max_pixels)

    def test_bad_directories_reported_without_hiding_good_ones(self):
        bad = os.path.join(self.tmp, "models", "Bad")
        _touch(os.path.join(bad, "a.gguf"))
        _touch(os.path.join(bad, "b.gguf"))
        _touch(os.path.join(bad, "mmproj.gguf"))
        _touch(os.path.join(self.tmp, "models", "NoProj", "a.gguf"))
        good = os.path.join(self.tmp, "models", "Good")
        _touch(os.path.join(good, "g.gguf"))
        _touch(os.path.join(good, "g-mmproj.gguf"))
        specs, problems = captioner.discover_models(self.tmp)
        self.assertEqual([s.name for s in specs], ["Good"])
        self.assertEqual(len(problems), 2)
        self.assertIn("Bad", problems[0])
        self.assertIn("NoProj", problems[1])

    def test_bad_model_ini_reported(self):
        d = os.path.join(self.tmp, "models", "Ini")
        _touch(os.path.join(d, "m.gguf"))
        _touch(os.path.join(d, "mmproj.gguf"))
        with open(os.path.join(d, "model.ini"), "w") as f:
            f.write("[model]\nmax_pixels = lots\n")
        specs, problems = captioner.discover_models(self.tmp)
        self.assertEqual(specs, [])
        self.assertIn("model.ini", problems[0])


class PromptStoreTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdir = os.path.join(self.tmp, "prompts")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_crud_roundtrip(self):
        self.assertEqual(captioner.list_prompts(self.pdir), {})
        captioner.save_prompt(self.pdir, "beta", "  second\n")
        captioner.save_prompt(self.pdir, "Alpha", "first")
        self.assertEqual(captioner.list_prompts(self.pdir),
                         {"Alpha": "first", "beta": "second"})
        self.assertEqual(sorted(os.listdir(self.pdir)), ["Alpha.txt", "beta.txt"])

        captioner.rename_prompt(self.pdir, "beta", "gamma")
        self.assertEqual(list(captioner.list_prompts(self.pdir)), ["Alpha", "gamma"])
        captioner.delete_prompt(self.pdir, "Alpha")
        self.assertEqual(list(captioner.list_prompts(self.pdir)), ["gamma"])

    def test_rename_onto_existing_raises(self):
        captioner.save_prompt(self.pdir, "a", "1")
        captioner.save_prompt(self.pdir, "b", "2")
        with self.assertRaises(ValueError):
            captioner.rename_prompt(self.pdir, "a", "b")
        self.assertEqual(captioner.read_prompt(self.pdir, "b"), "2")

    def test_invalid_names_rejected(self):
        for bad in ("", "   ", ".hidden", "a/b", "a\\b"):
            with self.assertRaises(ValueError, msg=bad):
                captioner.save_prompt(self.pdir, bad, "x")


class EncodeImageTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _decode(self, data_url: str) -> Image.Image:
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        return Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))

    def test_downscales_to_max_pixels_keeping_aspect(self):
        p = os.path.join(self.tmp, "big.jpg")
        Image.new("RGB", (2000, 1000)).save(p)
        img = self._decode(captioner.encode_image(p, 500_000))
        self.assertLessEqual(img.width * img.height, 500_000)
        self.assertAlmostEqual(img.width / img.height, 2.0, places=1)

    def test_small_image_untouched(self):
        p = os.path.join(self.tmp, "small.png")
        Image.new("RGB", (40, 30)).save(p)
        self.assertEqual(self._decode(captioner.encode_image(p, 1_000_000)).size, (40, 30))

    def test_applies_exif_rotation(self):
        p = os.path.join(self.tmp, "rot.jpg")
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90° CW to display
        Image.new("RGB", (40, 30)).save(p, exif=exif)
        self.assertEqual(self._decode(captioner.encode_image(p, 1_000_000)).size, (30, 40))

    def test_unreadable_raises_image_error(self):
        p = os.path.join(self.tmp, "bad.png")
        with open(p, "w") as f:
            f.write("not an image")
        with self.assertRaises(captioner.ImageError):
            captioner.encode_image(p, 1_000_000)


class LlamaServerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = _write_fake_server(self.tmp)
        self.spec = captioner.ModelSpec("Fake", self.bin, self.bin)  # any existing files
        self.img = os.path.join(self.tmp, "img.png")
        Image.new("RGB", (64, 48)).save(self.img)
        self.server = captioner.LlamaServer(self.bin)

    def tearDown(self):
        self.server.shutdown()
        os.environ.pop("FAKE_SERVER_MODE", None)
        shutil.rmtree(self.tmp)

    def test_load_caption_unload(self):
        self.assertIsNone(self.server.loaded_model)
        self.server.load(self.spec)
        self.assertEqual(self.server.loaded_model, "Fake")
        text = self.server.caption(self.img, "describe", captioner.CaptionParams())
        self.assertEqual(text, "describe|64x48")
        # Second load of the same spec reuses the running process.
        proc = self.server._proc
        self.server.load(self.spec)
        self.assertIs(self.server._proc, proc)
        self.server.unload()
        self.assertIsNone(self.server.loaded_model)
        self.assertIsNotNone(proc.poll())

    def test_switching_models_restarts(self):
        self.server.load(self.spec)
        proc = self.server._proc
        other = captioner.ModelSpec("Other", self.bin, self.bin)
        self.server.load(other)
        self.assertEqual(self.server.loaded_model, "Other")
        self.assertIsNot(self.server._proc, proc)
        self.assertIsNotNone(proc.poll())

    def test_crash_on_load_raises_with_log(self):
        os.environ["FAKE_SERVER_MODE"] = "crash"
        with self.assertRaises(captioner.ServerError) as cm:
            self.server.load(self.spec)
        self.assertIn("exited", str(cm.exception))
        self.assertIn("fake: model load failed", str(cm.exception))
        self.assertIsNone(self.server.loaded_model)

    def test_missing_files_raise(self):
        bad = captioner.ModelSpec("Bad", os.path.join(self.tmp, "nope.gguf"), self.bin)
        with self.assertRaises(captioner.ServerError):
            self.server.load(bad)
        with self.assertRaises(captioner.ServerError):
            captioner.LlamaServer(os.path.join(self.tmp, "missing")).load(self.spec)

    def test_caption_without_model_raises(self):
        with self.assertRaises(captioner.ServerError):
            self.server.caption(self.img, "x", captioner.CaptionParams())


if __name__ == "__main__":
    unittest.main()
