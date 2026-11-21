# SPDX-License-Identifier: MIT
"""Tests of nbtapps/make_layouts.py and of the layouts it made, committed under nbtapps/layouts/.

  python3 -m unittest discover -s nbtapps/tests

The tests of the committed layouts need only the standard library and always run: for each app and size, the XML
differs from the base only in the scaled values, each scaled by the rule; the image zip has the base's entries in
the base's order, each of the size the rule gives; SOURCE matches the upstream zip; nothing else is there.

The tests that run the generator (on synthetic input in a temporary folder) need Pillow, and are skipped with a
reason when it is missing. The GitHub workflow has no Pillow, so it runs the first kind only.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from fractions import Fraction
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
NBTAPPS_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(NBTAPPS_DIR)
sys.path.insert(0, NBTAPPS_DIR)
sys.path.insert(0, HERE)
import make_nbtpkg as m  # noqa: E402
import make_layouts as L  # noqa: E402   imports without Pillow
import test_make_nbtpkg as t  # noqa: E402   its synthetic plugin

try:
    from PIL import Image
    HAVE_PILLOW = True
except ImportError:
    HAVE_PILLOW = False

needs_pillow = unittest.skipUnless(HAVE_PILLOW, "needs Pillow (python3 -m pip install Pillow), which the workflow "
                                                "does not install")
QUIET = (lambda *args: None)

# The rule, written out here again rather than taken from the generator.
AXIS = {"20": "x", "9": "x", "21": "y", "10": "y"}
SIZE_IDS = {"9", "10"}
PROPERTY = re.compile(rb'<property id="([0-9]+)" value="([^"]*)"')


def rule(value, size, base="1280x480", pid="9"):
    (w, h), (bw, bh) = [tuple(int(n) for n in s.split("x")) for s in (size, base)]
    factor = Fraction(w, bw) if AXIS[pid] == "x" else Fraction(h, bh)
    return max(1 if pid in SIZE_IDS else 0, round(Fraction(value) * factor))


def blanked(xml):
    """The XML with the values to scale taken out, and those values: (skeleton, [(id, value)])."""
    values = [(pid.decode(), value.decode()) for pid, value in PROPERTY.findall(xml) if pid.decode() in AXIS]
    skeleton = PROPERTY.sub(lambda mo: mo.group(0) if mo.group(1).decode() not in AXIS
                            else b'<property id="' + mo.group(1) + b'" value=""', xml)
    return skeleton, values


def png_size(data):
    """(width, height, bit depth, colour type) from a PNG's header, with the standard library."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR", "not a PNG"
    return struct.unpack(">IIBB", data[16:26])


def zip_entries(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [(info, z.read(info)) for info in z.infolist()]


def tree_of(folder):
    """{relative path: bytes} of every file under folder."""
    out = {}
    for here, _, names in os.walk(folder):
        for name in names:
            full = os.path.join(here, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, folder).replace(os.sep, "/")] = f.read()
    return out


# ---- the committed layouts, with the standard library only ----------------------------------------------------

class CommittedLayoutTests(unittest.TestCase):
    """What is committed under nbtapps/layouts/, against the upstream zips in this working tree."""

    @classmethod
    def setUpClass(cls):
        cls.table = m.load_table(REPO_ROOT)
        cls.apps = [app for app in cls.table["apps"].values() if app["layouts"]]
        cls.base = {}
        for app in cls.apps:
            files, _ = m.read_plugin(REPO_ROOT, app)
            cls.base[app["folder"]] = files

    def path(self, *parts):
        return os.path.join(REPO_ROOT, *m.LAYOUTS.split("/"), *parts)

    def read(self, *parts):
        with open(self.path(*parts), "rb") as f:
            return f.read()

    def each(self):
        """[(app key, folder, size)] of every generated layout."""
        return [(app["key"], app["folder"], size) for app in self.apps for size in self.table["generated_sizes"]]

    def test_the_five_apps(self):
        self.assertEqual([app["folder"] for app in self.apps],
                         ["BoostDeck", "ShiftDeck", "EngineCodes", "MhdMaps", "Bootmod3Suite", "LaunchDeck"])

    def test_nothing_else_is_there(self):
        self.assertEqual(sorted(os.listdir(self.path())), sorted(app["folder"] for app in self.apps))
        for app in self.apps:
            folder = app["folder"]
            with self.subTest(app=app["key"]):
                self.assertEqual(sorted(os.listdir(self.path(folder))),
                                 sorted(["SOURCE"] + self.table["generated_sizes"]))
                expected = ["SOURCE"] + ["%s/%s" % (size, name) for size in self.table["generated_sizes"]
                                         for name in ("%s.xml" % folder, m.image_zip(folder))]
                self.assertEqual(sorted(tree_of(self.path(folder))), sorted(expected))
                for here, dirs, names in os.walk(self.path(folder)):
                    for name in dirs + names:
                        self.assertFalse(os.path.islink(os.path.join(here, name)), name)

    def test_source(self):
        for app in self.apps:
            folder = app["folder"]
            with self.subTest(app=app["key"]):
                files = self.base[folder]
                expected = "".join("%s  %s/%s\n" % (hashlib.sha256(files[n]).hexdigest(), folder, n)
                                   for n in ("%s.xml" % folder, m.image_zip(folder)))
                self.assertEqual(self.read(folder, "SOURCE").decode("ascii"), expected)

    def test_base_is_what_the_rule_was_checked_on(self):
        """Every property id is a known one, every value to scale a plain non-negative whole number, and the XML
        starts with the declaration and has no final newline."""
        for app in self.apps:
            xml = self.base[app["folder"]]["%s.xml" % app["folder"]]
            with self.subTest(app=app["key"]):
                self.assertTrue(xml.startswith(b"<?xml version='1.0' encoding='utf-8'?>\n"))
                self.assertFalse(xml.endswith(b"\n"))
                ids = {pid.decode() for pid, _ in PROPERTY.findall(xml)}
                self.assertLessEqual(ids, {"2", "3", "9", "10", "20", "21", "23"})
                self.assertEqual(len(PROPERTY.findall(xml)), xml.count(b"<property "))
                for _, value in blanked(xml)[1]:
                    self.assertRegex(value, r"\A(0|[1-9][0-9]*)\Z")

    def test_xml_differs_only_by_the_rule(self):
        for key, folder, size in self.each():
            with self.subTest(app=key, size=size):
                self.check_xml(folder, size)

    def check_xml(self, folder, size):
        base = self.base[folder]["%s.xml" % folder]
        made = self.read(folder, size, "%s.xml" % folder)
        base_skeleton, base_values = blanked(base)
        made_skeleton, made_values = blanked(made)
        self.assertEqual(made_skeleton, base_skeleton)
        self.assertEqual([pid for pid, _ in made_values], [pid for pid, _ in base_values])
        self.assertEqual([value for _, value in made_values],
                         [str(rule(int(value), size, self.table["base_size"], pid)) for pid, value in base_values])
        self.assertGreater(len(made_values), 0)
        m.check_description("%s/%s.xml" % (size, folder), made)

    def test_image_zips_by_the_rule(self):
        for key, folder, size in self.each():
            with self.subTest(app=key, size=size):
                self.check_images(folder, size)

    def check_images(self, folder, size):
        base = zip_entries(self.base[folder][m.image_zip(folder)])
        made_data = self.read(folder, size, m.image_zip(folder))
        m.check_image_zip("%s/%s" % (size, m.image_zip(folder)), made_data)
        made = zip_entries(made_data)
        self.assertEqual([i.filename for i, _ in made], [i.filename for i, _ in base])
        for (info, data), (_, base_data) in zip(made, base):
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED, info.filename)
            width, height, depth, colour = png_size(data)
            base_w, base_h, _, _ = png_size(base_data)
            expected = (rule(base_w, size, pid="9"), rule(base_h, size, pid="10"))
            self.assertEqual((width, height), expected, info.filename)
            self.assertEqual((depth, colour), (8, 6), "%s is not 8-bit RGBA" % info.filename)
            if expected == (base_w, base_h):
                self.assertEqual(data, base_data, "%s keeps its size and is not a copy" % info.filename)

    def test_the_build_accepts_them(self):
        """make_nbtpkg.read_layouts takes them, with stand-in hook templates (the hook step writes the real ones)."""
        work = tempfile.mkdtemp(prefix="make-layouts-build-")
        self.addCleanup(shutil.rmtree, work, True)
        os.makedirs(os.path.join(work, "nbtapps", "hooks"))
        os.symlink(self.path(), os.path.join(work, "nbtapps", "layouts"))
        for name, text in (("select-layout.sh.in", t.SELECT_HOOK), ("remove-layout.sh.in", t.REMOVE_HOOK)):
            with open(os.path.join(work, "nbtapps", "hooks", name), "w") as f:
                f.write(text)
        for app in self.apps:
            with self.subTest(app=app["key"]):
                layouts = m.read_layouts(work, self.table, app, self.base[app["folder"]])
                self.assertEqual(list(layouts), m.all_sizes(self.table))


# ---- the rule and Pillow, with the standard library only ------------------------------------------------------

class RuleTests(unittest.TestCase):
    def test_rounding(self):
        cases = [
            (1172, Fraction(800, 1280), 0, 732),     # 732.5: halves go to the even number
            (58, Fraction(960, 1280), 0, 44),        # 43.5
            (76, Fraction(540, 480), 0, 86),         # 85.5
            (1, Fraction(720, 480), 1, 2),           # 1.5
            (5, Fraction(1, 2), 0, 2),               # 2.5
            (7, Fraction(1, 2), 0, 4),               # 3.5
            (1, Fraction(1, 4), 0, 0),               # a position may be 0
            (1, Fraction(1, 4), 1, 1),               # a width or height is at least 1
            (0, Fraction(3, 2), 0, 0),
        ]
        for value, factor, minimum, expected in cases:
            with self.subTest(value=value, factor=factor):
                self.assertEqual(L.scaled(value, factor, minimum), expected)

    def test_factors(self):
        table = {"base_size": "1280x480"}
        self.assertEqual(L.factors(table, "800x480"), (Fraction(5, 8), 1))
        self.assertEqual(L.factors(table, "1440x540"), (Fraction(9, 8), Fraction(9, 8)))
        self.assertEqual(L.scaled_size(1, 1, *L.factors(table, "1920x720")), (2, 2))

    def test_without_pillow(self):
        with mock.patch.dict(sys.modules, {"PIL": None}):
            with self.assertRaises(m.Refused) as caught:
                L.pillow()
        self.assertIn("needs Pillow", str(caught.exception))

    def test_road_jet_and_beamer_get_no_layouts(self):
        table = m.load_table(REPO_ROOT)
        for key in ("road-jet", "beamer", "xdrive-control", "hud-gauges"):
            with self.subTest(key=key):
                with self.assertRaises(m.Refused) as caught:
                    L.layout_apps(table, [key])
                self.assertIn("has no layout per screen size", str(caught.exception))
        self.assertRaises(m.Refused, L.layout_apps, table, ["no-such-app"])


# ---- the generator on synthetic input, with Pillow ------------------------------------------------------------

BASE_XML = """<?xml version='1.0' encoding='utf-8'?>
<pluginApps>
  <pluginApp applicationType="VehicleInformation" text="Delta 1280x480">
    <entryButton id="1" action="10">
      <properties>
        <property id="23" value="2" />
      </properties>
    </entryButton>
    <hmiStates>
      <hmiState id="101" text="value 20">
        <components>
          <image id="3" model="95">
            <properties>
              <property id="20" value="{x1}" />
              <property id="21" value="{y1}" />
              <property id="9" value="{w1}" />
              <property id="10" value="{h1}" />
              <property id="2" value="false" />
              <property id="3" value="true" />
            </properties>
          </image>
          <label id="4" model="80">
            <properties>
              <property  id="20"   value="{x2}"/>
              <property value="{y2}" id="21" />
              <property id="9" value="{w2}" />
              <property id="10" value="{h2}" />
            </properties>
          </label>
        </components>
      </hmiState>
    </hmiStates>
  </pluginApp>
</pluginApps>"""

# The values of BASE_XML at each size, worked out by hand.
VALUES = {
    "1280x480": dict(x1=1172, y1=76, w1=58, h1=1, x2=0, y2=9, w2=1, h2=400),
    "800x480": dict(x1=732, y1=76, w1=36, h1=1, x2=0, y2=9, w2=1, h2=400),     # 732.5, 36.25, 0.625
    "960x480": dict(x1=879, y1=76, w1=44, h1=1, x2=0, y2=9, w2=1, h2=400),     # 43.5, 0.75
    "1440x540": dict(x1=1318, y1=86, w1=65, h1=1, x2=0, y2=10, w2=1, h2=450),  # 1318.5, 85.5, 65.25, 1.125, 10.125
    "1920x720": dict(x1=1758, y1=114, w1=87, h1=2, x2=0, y2=14, w2=2, h2=600),  # 1.5 (h1, w2), 13.5
    "320x240": dict(x1=293, y1=38, w1=14, h1=1, x2=0, y2=4, w2=1, h2=200),     # 14.5, 0.5, 4.5, 0.25
}
SIZES = ["800x480", "960x480", "1440x540", "1920x720", "320x240"]


def png(image, **options):
    out = io.BytesIO()
    image.save(out, "PNG", **options)
    return out.getvalue()


def make_zip(entries, method=zipfile.ZIP_DEFLATED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as z:
        for name, data in entries:
            z.writestr(name, data)
    return out.getvalue()


def sample_images():
    """[(name, bytes)], not in name order: a gradient, a 1x1, a palette image and one with an ICC profile."""
    gradient = Image.new("RGBA", (58, 76))
    gradient.putdata([(x * 4, y * 3, (x * y) % 256, 128 + (x % 2) * 127) for y in range(76) for x in range(58)])
    tiny = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    palette = Image.new("P", (8, 4))
    palette.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255] * 64)
    palette.putdata([(x + y) % 4 for y in range(4) for x in range(8)])
    profiled = Image.new("RGBA", (40, 20), (10, 200, 30, 255))
    return [("10.png", png(gradient)), ("2.png", png(tiny)), ("1.png", png(palette)),
            ("300.png", png(profiled, icc_profile=b"\0" * 128))]


@needs_pillow
class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="make-layouts-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.table = {"namespace": "com.example.test", "requires_agent": ">=1.6.0", "plugins_root": t.PLUGINS_ROOT,
                      "base_size": "1280x480", "generated_sizes": list(SIZES), "apps": [
                          {"key": "delta", "name": "Delta", "folder": "Delta", "zip": "packages/two.zip",
                           "workers": ["delta-qnx"], "connector": 303, "images": True, "layouts": True},
                          {"key": "alpha", "name": "Alpha", "folder": "Alpha", "zip": "packages/two.zip",
                           "workers": ["alpha-qnx"], "connector": 301, "images": True, "layouts": False}]}
        self.images = sample_images()
        self.xml = BASE_XML.format(**VALUES["1280x480"]).encode()
        self.write_base()

    def write_base(self, xml=None, images=None):
        members = dict(t.plugin("Delta", ["delta-qnx"], 303))
        members.update(t.plugin("Alpha", ["alpha-qnx"], 301))
        members["Delta/Delta.xml"] = self.xml if xml is None else xml
        members["Delta/Delta_ALL_Images.zip"] = make_zip(self.images) if images is None else images
        self.base = members
        self.put("nbtapps/apps.json", json.dumps(self.table).encode())
        self.put("packages/two.zip", make_zip(members.items()))

    def put(self, relative, data):
        full = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)

    def layouts(self, *parts):
        return os.path.join(self.root, "nbtapps", "layouts", *parts)

    def read(self, *parts):
        with open(self.layouts(*parts), "rb") as f:
            return f.read()

    def generate(self, keys=None):
        return L.run(self.root, keys, False, log=QUIET)

    def check(self, keys=None):
        return L.run(self.root, keys, True, log=QUIET)

    def refused(self, call, *needles):
        with self.assertRaises(m.Refused) as caught:
            call()
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    # -- what it makes

    def test_xml_by_the_rule_and_nothing_else(self):
        self.generate()
        for size in SIZES:
            with self.subTest(size=size):
                self.assertEqual(self.read("Delta", size, "Delta.xml").decode(), BASE_XML.format(**VALUES[size]))

    def test_images_by_the_rule(self):
        self.generate()
        base = dict(self.images)
        for size in SIZES:
            fx, fy = L.factors(self.table, size)
            with self.subTest(size=size):
                made = zip_entries(self.read("Delta", size, "Delta_ALL_Images.zip"))
                self.assertEqual([i.filename for i, _ in made], ["10.png", "2.png", "1.png", "300.png"])
                for info, data in made:
                    self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
                    self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                    original = Image.open(io.BytesIO(base[info.filename]))
                    image = Image.open(io.BytesIO(data))
                    expected = (max(1, round(original.width * fx)), max(1, round(original.height * fy)))
                    self.assertEqual(image.size, expected, info.filename)
                    if expected == original.size:
                        self.assertEqual(data, base[info.filename], "%s is not copied as it is" % info.filename)
                        continue
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.mode, "RGBA")
                    lanczos = original.convert("RGBA").resize(expected, Image.Resampling.LANCZOS)
                    self.assertEqual(image.tobytes(), lanczos.tobytes(), info.filename)
                    self.assertEqual(image.info.get("icc_profile"), original.info.get("icc_profile"))

    def test_unchanged_size_is_copied_byte_for_byte(self):
        self.generate()
        tiny = dict(self.images)["2.png"]
        for size in ("800x480", "960x480", "1440x540", "320x240"):
            with self.subTest(size=size):
                made = dict((i.filename, d) for i, d in zip_entries(self.read("Delta", size, "Delta_ALL_Images.zip")))
                self.assertEqual(made["2.png"], tiny)
        made = dict((i.filename, d) for i, d in zip_entries(self.read("Delta", "1920x720", "Delta_ALL_Images.zip")))
        self.assertEqual(png_size(made["2.png"])[:2], (2, 2))     # round(1.5) is 2

    def test_source_and_layout(self):
        self.generate()
        expected = "".join("%s  Delta/%s\n" % (hashlib.sha256(self.base["Delta/" + n]).hexdigest(), n)
                           for n in ("Delta.xml", "Delta_ALL_Images.zip"))
        self.assertEqual(self.read("Delta", "SOURCE").decode(), expected)
        self.assertEqual(sorted(os.listdir(self.layouts())), ["Delta"])
        self.assertEqual(sorted(tree_of(self.layouts("Delta"))),
                         sorted(["SOURCE"] + ["%s/%s" % (s, n) for s in SIZES
                                              for n in ("Delta.xml", "Delta_ALL_Images.zip")]))

    def test_same_input_same_bytes(self):
        self.generate()
        first = tree_of(self.layouts())
        self.generate()
        self.assertEqual(tree_of(self.layouts()), first)

    def test_rewrite_removes_what_was_there(self):
        self.generate()
        self.put("nbtapps/layouts/Delta/800x480/old.txt", b"x")
        self.put("nbtapps/layouts/Delta/1024x600/Delta.xml", b"x")
        self.generate(["delta"])
        self.assertNotIn("800x480/old.txt", tree_of(self.layouts("Delta")))
        self.assertFalse(os.path.exists(self.layouts("Delta", "1024x600")))
        self.assertEqual(sorted(os.listdir(self.layouts())), ["Delta"])

    # -- --check

    def test_check_matches(self):
        self.generate()
        before = tree_of(self.layouts())
        self.assertEqual(self.check(), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            L.main(["--check"], root=self.root)          # no SystemExit
        self.assertIn("Delta: the committed layouts are what the generator makes", out.getvalue())
        self.assertEqual(tree_of(self.layouts()), before)

    def test_check_compares_pixels_not_bytes(self):
        self.generate()
        path = ("Delta", "800x480", "Delta_ALL_Images.zip")
        entries = [(i.filename, d) for i, d in zip_entries(self.read(*path))]
        recoded = [(n, png(Image.open(io.BytesIO(d)), compress_level=1)) for n, d in entries]
        self.assertNotEqual([d for _, d in recoded], [d for _, d in entries])
        self.put("nbtapps/layouts/Delta/800x480/Delta_ALL_Images.zip", make_zip(recoded, zipfile.ZIP_STORED))
        self.assertEqual(self.check(), [])

    def test_check_names_each_difference(self):
        def pixel_changed(entries):
            out = []
            for name, data in entries:
                if name == "10.png":
                    image = Image.open(io.BytesIO(data)).convert("RGBA")
                    image.putpixel((3, 3), (1, 2, 3, 4))
                    data = png(image)
                out.append((name, data))
            return out

        def resized(entries):
            return [(n, png(Image.open(io.BytesIO(d)).resize((5, 5))) if n == "10.png" else d) for n, d in entries]

        zip_path = "nbtapps/layouts/Delta/800x480/Delta_ALL_Images.zip"
        cases = [
            (lambda: self.put("nbtapps/layouts/Delta/960x480/Delta.xml",
                              self.read("Delta", "960x480", "Delta.xml") + b" "),
             "Delta/960x480/Delta.xml differs"),
            (lambda: self.put(zip_path, make_zip(pixel_changed(self.entries(zip_path)))),
             "800x480/Delta_ALL_Images.zip: 10.png: its pixels differ"),
            (lambda: self.put(zip_path, make_zip(resized(self.entries(zip_path)))), "10.png is 5x5, not 36x76"),
            (lambda: self.put(zip_path, make_zip(list(reversed(self.entries(zip_path))))), "the order differs"),
            (lambda: self.put(zip_path, make_zip(self.entries(zip_path)[1:])), "lacks 10.png"),
            (lambda: self.put(zip_path, b"not a zip"), "is not a readable zip"),
            (lambda: self.put("nbtapps/layouts/Delta/800x480/notes.txt", b"x"),
             "Delta/800x480/notes.txt is there and should not be"),
            (lambda: shutil.rmtree(self.layouts("Delta", "1920x720")), "Delta/1920x720/ is not there"),
            (lambda: self.put("nbtapps/layouts/Alpha/SOURCE", b"x"), "nbtapps/layouts/Alpha is there and is no"),
            (lambda: self.put("nbtapps/layouts/Delta/SOURCE", b"x"), "Delta/SOURCE differs"),
            (lambda: self.write_base(xml=self.xml.replace(b'value="58"', b'value="60"')), "Delta/SOURCE differs"),
        ]
        for change, needle in cases:
            with self.subTest(needle=needle):
                shutil.rmtree(self.layouts(), ignore_errors=True)
                self.write_base()
                self.generate()
                change()
                before = tree_of(self.layouts())
                problems = self.check()
                self.assertTrue(any(needle in p for p in problems), problems)
                self.assertEqual(tree_of(self.layouts()), before)        # --check writes nothing
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        L.main(["--check"], root=self.root)
                self.assertIn("--check found", str(caught.exception))
                self.assertIn(needle, err.getvalue())

    def entries(self, relative):
        with open(os.path.join(self.root, *relative.split("/")), "rb") as f:
            return [(i.filename, d) for i, d in zip_entries(f.read())]

    def test_check_with_nothing_committed(self):
        self.assertEqual(self.check(), ["nbtapps/layouts/Delta is not there"])

    # -- refusals

    def assert_nothing_written(self):
        self.assertFalse(os.path.exists(self.layouts()) and os.listdir(self.layouts()))

    def test_refuses_a_value_that_is_not_a_whole_number(self):
        for value in ("12.5", "-3", "", "1e3", " 12", "012", "0x10", "+4"):
            with self.subTest(value=value):
                self.write_base(xml=self.xml.replace(b'value="58"', b'value="%s"' % value.encode()))
                self.refused(self.generate, 'Delta/Delta.xml: <property id="9"> has the value',
                             "not a plain non-negative whole number")
                self.assert_nothing_written()

    def test_refuses_what_it_does_not_know(self):
        cases = [
            (b'<property id="23" value="2" />', b'<property id="7" value="2" />', 'property id="7">, which this'),
            (b'<property id="23" value="2" />', b"<property id='23' value='2' />", "is not a <property"),
            (b'<property id="23" value="2" />', b'<property id="23" value="2" unit="px" />', "is not a <property"),
            (b'<property id="23" value="2" />', b'<property id="23" />', "is not a <property"),
            (b'<property id="23" value="2" />', b'<property id="23" value="2" id="23" />', "not well-formed"),
            (b"<properties>", b'<properties><!-- <property id="20" value="1" /> -->', "the XML parser sees"),
            (b"<pluginApps>", b"<!DOCTYPE x><pluginApps>", "document type"),
        ]
        for old, new, needle in cases:
            with self.subTest(needle=needle):
                self.assertIn(old, self.xml)
                self.write_base(xml=self.xml.replace(old, new, 1))
                self.refused(self.generate, needle)
                self.assert_nothing_written()

    def test_refuses_an_entry_that_is_not_a_png(self):
        gif = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
        broken = dict(self.images)["10.png"][:60]
        cases = [
            (self.images + [("5.png", gif)], "Delta/Delta_ALL_Images.zip: 5.png is not a PNG"),
            (self.images + [("6.png", broken)], "6.png cannot be decoded as a PNG"),
            (self.images + [("readme.txt", b"x")], "holds 'readme.txt'"),
            (self.images + [("7.PNG", dict(self.images)["2.png"])], "holds '7.PNG'"),
            (self.images + [("img/8.png", dict(self.images)["2.png"])], "holds 'img/8.png'"),
        ]
        for images, needle in cases:
            with self.subTest(needle=needle):
                self.write_base(images=make_zip(images))
                self.refused(self.generate, needle)
                self.assert_nothing_written()

    def test_refuses_apps_without_layouts(self):
        self.refused(lambda: self.generate(["alpha"]), "alpha has no layout per screen size")
        self.refused(lambda: self.generate(["zulu"]), "'zulu' is no app")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            L.main(["alpha"], root=self.root)
        self.assertIn("make_layouts: alpha has no layout", str(caught.exception))
        self.assert_nothing_written()

    def test_refuses_what_the_build_refuses(self):
        """The base goes through make_nbtpkg's checks first."""
        self.write_base(xml=self.xml.replace(b"<pluginApp ", b'<pluginApp entryButtonFocusID="1" '))
        self.refused(self.generate, "entryButtonFocusID")
        self.assert_nothing_written()

    # -- nothing half-written

    def test_failure_while_writing_leaves_the_folder_as_it_was(self):
        self.generate()
        before = tree_of(self.layouts())
        table = m.load_table(self.root)
        app = table["apps"]["delta"]
        out = {"SOURCE": b"new", "800x480/Delta.xml": None}       # None cannot be written
        with self.assertRaises(TypeError):
            L.write_app(self.root, app, out)
        self.assertEqual(tree_of(self.layouts()), before)
        self.assertEqual(os.listdir(self.layouts()), ["Delta"])

    def test_failure_while_moving_leaves_the_folder_as_it_was(self):
        self.generate()
        before = tree_of(self.layouts())
        real = os.rename
        calls = []

        def rename(src, dst):
            calls.append(src)
            if len(calls) == 2:          # the new folder into place
                raise OSError("simulated")
            return real(src, dst)

        with mock.patch.object(L.os, "rename", rename):
            self.assertRaises(OSError, self.generate)
        self.assertEqual(tree_of(self.layouts()), before)
        self.assertEqual(os.listdir(self.layouts()), ["Delta"])

    def test_first_write_that_fails_leaves_nothing(self):
        with mock.patch.object(L.os, "rename", side_effect=OSError("simulated")):
            self.assertRaises(OSError, self.generate)
        self.assertEqual(os.listdir(self.layouts()), [])


if __name__ == "__main__":
    unittest.main()
