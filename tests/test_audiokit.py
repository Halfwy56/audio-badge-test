"""Тесты toolkit: python -m unittest discover -s tests -v   (только stdlib)."""
import io, os, shutil, sys, tempfile, threading, unittest, urllib.request, wave
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import audiokit, mock_backend  # noqa: E402


def wr(path, data):
    with open(path, "wb") as f:
        f.write(data)


def rd(path):
    with open(path, "rb") as f:
        return f.read()


class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        with redirect_stdout(io.StringIO()):
            for d in ("none", "clip", "silence", "dc", "quiet"):
                audiokit.gen(os.path.join(cls.dir, d + ".wav"), d, sec=3)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def chk(self, name):
        return audiokit.check(os.path.join(self.dir, name))

    def test_clean_is_ok(self):
        r = self.chk("none.wav")
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["warnings"], [])

    def test_each_defect_detected_once(self):
        expect = {"clip.wav": "клиппинг", "silence.wav": "тишина", "dc.wav": "DC", "quiet.wav": "тихий"}
        for f, word in expect.items():
            r = self.chk(f)
            self.assertEqual(r["status"], "warn", r)
            self.assertEqual(len(r["warnings"]), 1, r)             # ровно один, без побочных срабатываний
            self.assertIn(word, r["warnings"][0])

    def test_metric_values(self):
        self.assertGreater(self.chk("clip.wav")["clipped_ratio"], 0.1)
        self.assertGreater(self.chk("silence.wav")["silence_ratio"], 0.9)
        self.assertAlmostEqual(self.chk("dc.wav")["dc_offset"], 0.1, places=2)
        self.assertLess(self.chk("quiet.wav")["peak_dbfs"], audiokit.LOW_PEAK_DBFS)

    def test_stereo_first_channel_and_48k(self):
        p = os.path.join(self.dir, "st.wav")
        with wave.open(p, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
            with wave.open(os.path.join(self.dir, "none.wav")) as src:
                mono = src.readframes(48000)
            import array
            a = array.array("h", mono); st = array.array("h")
            for x in a:
                st.extend((x, 0))                                  # правый канал пустой — не должен влиять
            w.writeframes(st.tobytes())
        r = audiokit.check(p)
        self.assertEqual(r["status"], "ok", r); self.assertEqual(r["channels"], 2)

    def test_not_wav_is_fail_not_crash(self):
        for content in (b"", b"hello", b"RIFF\x00\x00\x00\x00WAVE"):
            p = os.path.join(self.dir, "bad.wav")
            wr(p, content)
            r = audiokit.check(p)
            self.assertEqual(r["status"], "fail", r); self.assertTrue(r["issues"])
        r = audiokit.check(os.path.join(self.dir, "missing.wav"))
        self.assertEqual(r["status"], "fail")

    def test_8bit_is_fail(self):
        p = os.path.join(self.dir, "u8.wav")
        with wave.open(p, "wb") as w:
            w.setnchannels(1); w.setsampwidth(1); w.setframerate(16000); w.writeframes(b"\x80" * 1600)
        self.assertEqual(audiokit.check(p)["status"], "fail")

    def test_cli_exit_codes(self):
        argv = sys.argv
        try:
            for files, code in ((["none.wav"], 0), (["none.wav", "clip.wav"], 1), (["clip.wav", "bad.wav"], 2)):
                wr(os.path.join(self.dir, "bad.wav"), b"x")
                sys.argv = ["audiokit", "check", "--json"] + [os.path.join(self.dir, f) for f in files]
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(audiokit.main(), code)
        finally:
            sys.argv = argv


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(); self.recv = os.path.join(self.dir, "received")
        self.srv = None
        with redirect_stdout(io.StringIO()):
            audiokit.gen(os.path.join(self.dir, "a.wav"), "none", sec=3)   # 96 KB → 2 чанка по 64 KB

    def start(self, fail_after=0):
        self.srv = mock_backend.make_server(0, self.recv, fail_after, quiet=True)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        if self.srv:
            self.srv.shutdown(); self.srv.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def up(self, name="a.wav", **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            code = audiokit.upload_one(os.path.join(self.dir, name), self.url, **kw)
        return code, out.getvalue()

    def same(self, a, b):
        return rd(a) == rd(b)

    def test_plain_upload(self):
        self.start()
        code, out = self.up()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.same(os.path.join(self.dir, "a.wav"), os.path.join(self.recv, "a.wav")))

    def test_resume_after_drop(self):
        self.start(fail_after=1)
        code, out = self.up()
        self.assertEqual(code, 0, out); self.assertIn("connection lost", out)
        self.assertTrue(self.same(os.path.join(self.dir, "a.wav"), os.path.join(self.recv, "a.wav")))

    def test_corrupted_part_is_reset_and_retried(self):
        self.start()
        sha = audiokit._sha256_file(os.path.join(self.dir, "a.wav"))
        urllib.request.urlopen(urllib.request.Request(f"{self.url}/uploads/{sha}?name=a.wav&size=96044",
                                                      method="PUT")).close()
        os.makedirs(self.recv, exist_ok=True)
        wr(os.path.join(self.recv, sha[:16] + ".part"), b"\0" * 96044)   # битый хвост
        code, out = self.up()
        self.assertEqual(code, 0, out); self.assertIn("retrying from scratch", out)
        self.assertTrue(self.same(os.path.join(self.dir, "a.wav"), os.path.join(self.recv, "a.wav")))
        self.assertFalse(os.path.exists(os.path.join(self.recv, sha[:16] + ".part")))

    def test_name_with_space_and_cyrillic(self):
        self.start()
        shutil.copy(os.path.join(self.dir, "a.wav"), os.path.join(self.dir, "смена 1.wav"))
        code, out = self.up("смена 1.wav")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.recv, "смена 1.wav")))

    def test_path_traversal_rejected(self):
        self.start()
        sha = "0" * 64
        for bad in ("../x.wav", "..", "a/../../x.wav", "%2e%2e%2fx.wav"):
            req = urllib.request.Request(f"{self.url}/uploads/{sha}?name={bad}&size=1", method="PUT")
            try:
                r = urllib.request.urlopen(req); code = r.status; body = r.read()
            except urllib.error.HTTPError as e:
                code = e.code; body = e.read()
            if code == 200:                                        # допустимо только если имя стало базовым
                self.assertNotIn(b"..", body)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x.wav")))

    def test_same_name_different_content_not_overwritten(self):
        self.start()
        self.assertEqual(self.up()[0], 0)
        os.makedirs(os.path.join(self.dir, "b"))
        with redirect_stdout(io.StringIO()):
            audiokit.gen(os.path.join(self.dir, "b", "a.wav"), "dc", sec=3)   # другой файл с тем же именем
        code, out = self.up(os.path.join("b", "a.wav"))
        self.assertEqual(code, 0, out)
        names = sorted(os.listdir(self.recv))
        self.assertEqual(len(names), 2, names); self.assertIn("a.wav", names)

    def test_backend_down_returns_2_without_traceback(self):
        self.url = "http://127.0.0.1:1"
        code, out = self.up()
        self.assertEqual(code, 2); self.assertIn("unreachable", out)

    def test_upload_with_check_skips_fail(self):
        self.start()
        wr(os.path.join(self.dir, "bad.wav"), b"nope")
        out = io.StringIO()
        with redirect_stdout(out):
            code = audiokit.upload([os.path.join(self.dir, "bad.wav"), os.path.join(self.dir, "a.wav")],
                                   self.url, precheck=True)
        self.assertEqual(code, 2); self.assertIn("skipped", out.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.recv, "a.wav")))
        self.assertFalse(os.path.exists(os.path.join(self.recv, "bad.wav")))


if __name__ == "__main__":
    unittest.main()
