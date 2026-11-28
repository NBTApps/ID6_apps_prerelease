# SPDX-License-Identifier: MIT
"""Tests of nbtapps/make_nbtpkg.py. Standard library only.

  python3 -m unittest discover -s nbtapps/tests

The tests that build or sign an archive need NBTApps' tools/nbtpkg.py: set NBTAPPS_HEADUNIT to the headunit/
folder of an NBTApps checkout. With it unset those tests are skipped, and say so; with it set to a folder that is
not such a checkout, they fail. The tests that sign also need the `cryptography` module. The end-to-end test also
checks each archive with the catalogue's tools/nbtcat.py when NBTCAT_TOOLS names the folder that holds it, and
skips that part otherwise. The GitHub workflow sets NBTAPPS_HEADUNIT, so nothing that matters is skipped there.

Every key here is a throwaway one, made in a temporary folder for the test.
"""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
NBTAPPS_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(NBTAPPS_DIR)
sys.path.insert(0, NBTAPPS_DIR)
import make_nbtpkg as m  # noqa: E402

HEADUNIT = os.environ.get(m.HEADUNIT_ENV)
NBTPKG = m.load_nbtpkg(HEADUNIT) if HEADUNIT else None   # a bad folder fails here, loudly
try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

needs_nbtpkg = unittest.skipUnless(NBTPKG, "set %s to the headunit/ folder of an NBTApps checkout" % m.HEADUNIT_ENV)
needs_keys = unittest.skipUnless(NBTPKG and HAVE_CRYPTO, "needs %s and the cryptography module" % m.HEADUNIT_ENV)
needs_git = unittest.skipUnless(shutil.which("git"), "needs git")

PLUGINS_ROOT = "/var/opt/hmi/data/ID5/Plugins"
QUIET = (lambda *args: None)


def load_nbtcat():
    folder = os.environ.get("NBTCAT_TOOLS")
    if not folder:
        return None
    spec = importlib.util.spec_from_file_location("nbtcat_for_tests", os.path.join(folder, "nbtcat.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- synthetic plugins ----------------------------------------------------------------------------------------

def elf(e_type, body=b"", bits=1, order=1, machine=m.EM_ARM):
    header = b"\x7fELF" + bytes([bits, order, 1, 0]) + bytes(8) + struct.pack("<HH", e_type, machine)
    return header + bytes(52 - len(header)) + body


def plugin_manifest(folder, connector, images=True, extra=""):
    image = "<ImageDBFile>{pluginDir}/%s_ALL_Images.zip</ImageDBFile>" % folder if images else ""
    return ("<?xml version='1.0' encoding='utf-8'?>\n<plugin><name>%s</name><type>App</type>"
            "<file>{pluginDir}/%s</file><config><autoStart>yes</autoStart><dependencies><pluginAppConnector>"
            "<type>Etch</type><id>%d</id></pluginAppConnector></dependencies><parameters>"
            "<DescFile>{pluginDir}/%s.xml</DescFile>%s</parameters>%s</config></plugin>\n"
            % (folder, folder, connector, folder, image, extra)).encode()


def description(text="base"):
    return ("<?xml version='1.0'?>\n<pluginApps><pluginApp name='%s'/></pluginApps>\n" % text).encode()


def images(text="base"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("img/%s.png" % text, b"\x89PNG not really " + text.encode())
    return out.getvalue()


def plugin(folder, workers, connector, with_images=True):
    paths = b"".join(b"pad\0%s/%s/%s\0" % (PLUGINS_ROOT.encode(), folder.encode(), w.encode()) for w in workers)
    files = {"lib%s.so" % folder: elf(m.ET_DYN, paths),
             "%s.manifest" % folder: plugin_manifest(folder, connector, with_images),
             "%s.xml" % folder: description()}
    for w in workers:
        files[w] = elf(m.ET_EXEC, b"worker " + w.encode())
    if with_images:
        files["%s_ALL_Images.zip" % folder] = images()
    return {"%s/%s" % (folder, name): data for name, data in files.items()}


TABLE = {
    "namespace": "com.example.test",
    "requires_agent": ">=1.6.0",
    "plugins_root": PLUGINS_ROOT,
    "base_size": "1280x480",
    "generated_sizes": ["800x480", "1920x720"],
    "base_shown_at": ["1440x540"],
    "apps": [
        {"key": "alpha", "name": "Alpha", "folder": "Alpha", "zip": "packages/one.zip",
         "workers": ["alpha-qnx", "alpha-helper-qnx"], "connector": 301, "images": True, "layouts": False,
         "licenses": ["source/third-party/T-LICENSE"]},
        {"key": "bravo", "name": "Bravo", "folder": "Bravo", "zip": "packages/one.zip",
         "workers": ["bravo-qnx"], "connector": 302, "images": False, "layouts": False},
        {"key": "delta", "name": "Delta", "folder": "Delta", "zip": "packages/two.zip",
         "workers": ["delta-qnx"], "connector": 303, "images": True, "layouts": True, "conflicts": ["alpha"]},
    ],
}

SELECT_HOOK = "#!/bin/sh\n# picks a layout of @FOLDER@ (default @BASE_SIZE@)\ncase \"$size\" in @SIZES@) ;; esac\n"
REMOVE_HOOK = "#!/bin/sh\nrm -f \"$NBT_VAR_PLUGINS_DIR/@FOLDER@/@FOLDER@.xml\"\n"


# What the real table says each app writes outside its folder (see nbtapps/README.md).
WRITTEN = {
    "boostdeck": ["/var/opt/hmi/data/ID5/BoostDeck.prefs", "/var/opt/hmi/data/ID5/BoostDeck.signals",
                  "/var/opt/hmi/data/ID5/ID6DctTemperature.prefs"],
    "shiftdeck": ["/var/opt/hmi/data/ID5/ShiftDeck.v2.prefs", "/var/opt/hmi/data/ID5/ShiftDeck.prefs"],
    "launchdeck": ["/var/opt/hmi/data/ID5/LaunchDeck.prefs", "/var/opt/hmi/data/ID5/LaunchDeck.records",
                   "/var/opt/hmi/data/ID5/LaunchDeck.60-130", "/var/opt/hmi/data/ID5/LaunchDeck.0-100",
                   "/var/opt/hmi/data/ID5/LaunchDeck.100-200"],
    "warmdeck": ["/var/opt/hmi/data/ID5/WarmDeck.units", "/var/opt/hmi/data/ID5/ID6DctTemperature.prefs"],
}


class Fixture:
    """A working tree in a temporary folder with apps.json, two upstream zips and the licence files."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="make-nbtpkg-test-")
        self.table = copy.deepcopy(TABLE)
        one = dict(plugin("Alpha", ["alpha-qnx", "alpha-helper-qnx"], 301))
        one.update(plugin("Bravo", ["bravo-qnx"], 302, with_images=False))
        one.update({"README.md": b"readme\n", "source/controls.c": b"int x;\n", "LICENSE-x.txt": b"MIT\n"})
        two = dict(plugin("Delta", ["delta-qnx"], 303))
        two["README.md"] = b"readme\n"
        self.zips = {"packages/one.zip": one, "packages/two.zip": two}
        self.sums = {"packages/one.zip": True, "packages/two.zip": False}   # True: computed; bytes: as given
        self.files = {"source/LICENSE": b"MIT License\n\nproject\n", "source/third-party/T-LICENSE": b"MIT T\n"}
        self.write()

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def put(self, relative, data):
        full = self.path(*relative.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)

    def write(self):
        self.put(m.TABLE, json.dumps(self.table, indent=2).encode())
        for relative, data in self.files.items():
            self.put(relative, data)
        for name, members in self.zips.items():
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for path, data in members.items():
                    z.writestr(path, data)
                sums = self.sums[name]
                if sums is True:
                    sums = "".join("%s  %s\n" % (hashlib.sha256(d).hexdigest(), p) for p, d in members.items()
                                   if "/" in p and not p.startswith("source/")).encode()
                if sums:
                    z.writestr(m.SUMS, sums)
            self.put(name, out.getvalue())

    def table_now(self):
        return m.load_table(self.root)

    def app(self, key):
        return self.table_now()["apps"][key]

    def read(self, key):
        table = self.table_now()
        files, _ = m.read_plugin(self.root, table["apps"][key])
        m.check_plugin(table, table["apps"][key], files)
        return files

    def add_layouts(self, folder="Delta", sizes=None, source=None):
        base = self.zips["packages/two.zip"]
        for size in sizes if sizes is not None else self.table["generated_sizes"]:
            self.put("%s/%s/%s/%s.xml" % (m.LAYOUTS, folder, size, folder), description(size))
            self.put("%s/%s/%s/%s_ALL_Images.zip" % (m.LAYOUTS, folder, size, folder), images(size))
        if source is None:
            source = "".join("%s  %s/%s\n" % (hashlib.sha256(base["%s/%s" % (folder, n)]).hexdigest(), folder, n)
                             for n in ("%s.xml" % folder, "%s_ALL_Images.zip" % folder)).encode()
        self.put("%s/%s/SOURCE" % (m.LAYOUTS, folder), source)

    def add_hooks(self, select=SELECT_HOOK, remove=REMOVE_HOOK):
        self.put("%s/select-layout.sh.in" % m.HOOKS, select.encode())
        self.put("%s/remove-layout.sh.in" % m.HOOKS, remove.encode())

    def git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.run(("git", "-C", self.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               "-c", "commit.gpgsign=false") + args, check=True, capture_output=True, env=env)

    def commit_all(self):
        if not os.path.isdir(self.path(".git")):
            self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.out = tempfile.mkdtemp(prefix="make-nbtpkg-out-")
        self.addCleanup(shutil.rmtree, self.out, True)

    def saving_alpha(self, template=True):
        """Alpha, an app without layouts, saving a file in its own folder and one outside it."""
        saved = [PLUGINS_ROOT + "/Alpha/alpha-on", "/var/a/Alpha.state"]
        self.fx.table["apps"][0]["written"] = saved
        lib = self.fx.zips["packages/one.zip"]["Alpha/libAlpha.so"]
        self.fx.zips["packages/one.zip"]["Alpha/libAlpha.so"] = lib + b"\0" + b"\0".join(p.encode() for p in saved) + b"\0"
        self.fx.write()
        if template:
            self.fx.put("%s/remove-saved.sh.in" % m.HOOKS, b"#!/bin/sh\n# @FOLDER@\nwritten=\"@WRITTEN@\"\n")
        return saved

    def refused(self, call, *needles):
        with self.assertRaises(m.Refused) as caught:
            call()
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def change(self, zip_name, path, data):
        if data is None:
            del self.fx.zips[zip_name][path]
        else:
            self.fx.zips[zip_name][path] = data
        self.fx.write()


def make_keys(folder):
    """A throwaway key pair in `folder`: (seed, public, key file, pub file)."""
    seed = os.urandom(32)
    public = NBTPKG.public_key(seed)
    key_file, pub_file = os.path.join(folder, "t.key"), os.path.join(folder, "t.pub")
    with open(key_file, "w") as f:
        f.write("# throwaway\n%s\n" % seed.hex())
    with open(pub_file, "w") as f:
        f.write("# throwaway\n%s\n" % public.hex())
    return seed, public, key_file, pub_file


# ---- the table ------------------------------------------------------------------------------------------------

class TableTests(FixtureCase):
    def test_real_table_loads(self):
        table = m.load_table(REPO_ROOT)
        self.assertEqual(len(table["apps"]), 12)
        self.assertNotIn("hud-gauges", table["apps"])
        self.assertEqual(table["apps"]["mg-flasher"]["conflicts"], ["mhd-suite", "bootmod3-suite"])
        self.assertEqual(table["apps"]["mhd-suite"]["id"], "com.github.nbtapps.mhd-suite")

    def test_refusals(self):
        cases = [
            (lambda t: t["apps"][0].update(colour="red"), "has the keys"),
            (lambda t: t["apps"][1].update(connector=301), "connector"),
            (lambda t: t["apps"][1].update(images=False, layouts=True), "layouts needs images"),
            (lambda t: t["apps"][2].update(conflicts=["zulu"]), "conflicts"),
            (lambda t: t["apps"][2].update(conflicts=["delta"]), "conflicts"),
            (lambda t: t["apps"][0].update(workers=["alpha"]), "workers"),
            (lambda t: t["apps"][0].update(zip="../one.zip"), "zip"),
            (lambda t: t["apps"][0].update(licenses=["source/../x"]), "licenses"),
            (lambda t: t["apps"][0].update(key="Alpha"), "key"),
            (lambda t: t.update(generated_sizes=["1280x480"]), "generated_sizes"),
            (lambda t: t.update(base_shown_at=["800x480"]), "base_shown_at"),          # 720 units, not 1260
            (lambda t: t.update(base_shown_at=["1280x480"]), "base_shown_at"),
            (lambda t: t.update(base_shown_at=["1920x720"]), "base_shown_at"),         # a generated size here
            (lambda t: t.update(base_shown_at=["1440x540", "1440x540"]), "base_shown_at"),
            (lambda t: t.update(base_shown_at="1440x540"), "base_shown_at"),
            (lambda t: t.pop("base_shown_at"), "has the keys"),
            (lambda t: t["apps"][2].update(base_layout_at=["1440x540"]), "base_layout_at"),
            (lambda t: t["apps"][2].update(base_layout_at=["1280x480"]), "base_layout_at"),
            (lambda t: t["apps"][2].update(base_layout_at=["800x480", "800x480"]), "base_layout_at"),
            (lambda t: t["apps"][2].update(base_layout_at="800x480"), "base_layout_at"),
            (lambda t: t["apps"][0].update(base_layout_at=["800x480"]), "base_layout_at is for an app with layouts"),
            (lambda t: t["apps"][2].update(written=["relative/file"]), "written"),
            (lambda t: t["apps"][2].update(written=["/var/x/../y"]), "written"),
            (lambda t: t["apps"][2].update(written=["/var/a b"]), "written"),
            (lambda t: t["apps"][2].update(written=["/var/x;rm"]), "written"),
            (lambda t: t["apps"][2].update(written=["/var/$x"]), "written"),
            (lambda t: t["apps"][2].update(written=["/var/x*"]), "written"),
            (lambda t: t["apps"][2].update(written=["/var/x/"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Alpha/x"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/deeper/x"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/libDelta.so"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/Delta.manifest"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/Delta.xml"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/Delta_ALL_Images.zip"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/delta-qnx"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/layouts"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/gateway-ip.txt"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT + "/Delta/.select-layout.xml"]), "written"),
            (lambda t: t["apps"][0].update(written=[PLUGINS_ROOT + "/Alpha/alpha-helper-qnx"]), "written"),
            (lambda t: t["apps"][2].update(written=[PLUGINS_ROOT]), "written"),
            (lambda t: t["apps"][2].update(written=["/usr/lib/x.so"]), "written"),
            (lambda t: t["apps"][2].update(written=["/dev/null"]), "written"),
            (lambda t: t["apps"][2].update(written=["/var/opt/hmi/data/ID5/gateway-ip.txt"]), "written"),
            (lambda t: t["apps"][2].update(written=["/var/x", "/var/x"]), "written"),
            (lambda t: t["apps"][2].update(written_before=["relative/file"]), "written_before"),
            (lambda t: t["apps"][2].update(written_before=["/var/x;rm"]), "written_before"),
            (lambda t: t["apps"][2].update(written_before=[PLUGINS_ROOT + "/Delta/libDelta.so"]), "written_before"),
            (lambda t: t["apps"][2].update(written_before=["/var/x", "/var/x"]), "written_before"),
            (lambda t: t["apps"][2].update(written_before="/var/x"), "written_before"),
            (lambda t: t["apps"][2].update(written=["/var/x"], written_before=["/var/x"]),
             "a path is in both written and written_before"),
        ]
        for mutate, needle in cases:
            with self.subTest(needle=needle):
                self.fx.table = copy.deepcopy(TABLE)
                mutate(self.fx.table)
                self.fx.write()
                self.refused(self.fx.table_now, needle)

    def test_real_table_overrides(self):
        table = m.load_table(REPO_ROOT)
        every = ["1280x480", "800x480", "960x480"]
        self.assertEqual(m.all_sizes(table), every)
        self.assertEqual(table["base_shown_at"], ["1440x540", "1920x720"])
        for key, app in table["apps"].items():
            with self.subTest(key=key):
                self.assertNotIn("base_layout_at", app)
                self.assertEqual(m.layout_sizes(table, app), every)
                self.assertEqual(app.get("written"), WRITTEN.get(key))
                self.assertIsNone(app.get("written_before"))


# ---- checks of the upstream plugin ----------------------------------------------------------------------------

class PluginTests(FixtureCase):
    def test_accepted(self):
        for key in ("alpha", "bravo", "delta"):
            with self.subTest(key=key):
                files = self.fx.read(key)
                self.assertEqual(sorted(files), sorted(m.plugin_files(self.fx.app(key))))

    def test_sums_note(self):
        _, note = m.read_plugin(self.fx.root, self.fx.app("alpha"))
        self.assertIn("SHA256SUMS matches all 6 files", note)
        _, note = m.read_plugin(self.fx.root, self.fx.app("delta"))
        self.assertIn("has no SHA256SUMS", note)

    def test_sums_mismatch(self):
        good = hashlib.sha256(self.fx.zips["packages/one.zip"]["Alpha/alpha-qnx"]).hexdigest()
        self.fx.zips["packages/one.zip"]["Alpha/alpha-qnx"] += b"changed"
        lines = "".join("%s  %s\n" % (good if p == "Alpha/alpha-qnx" else hashlib.sha256(d).hexdigest(), p)
                        for p, d in self.fx.zips["packages/one.zip"].items() if p.startswith("Alpha/"))
        self.fx.sums["packages/one.zip"] = lines.encode()
        self.fx.write()
        self.refused(lambda: self.fx.read("alpha"), "Alpha/alpha-qnx is not the file its SHA256SUMS pins")

    def test_sums_lacks_a_file(self):
        lines = "".join("%s  %s\n" % (hashlib.sha256(d).hexdigest(), p)
                        for p, d in self.fx.zips["packages/one.zip"].items()
                        if p.startswith("Alpha/") and p != "Alpha/Alpha.xml")
        self.fx.sums["packages/one.zip"] = lines.encode()
        self.fx.write()
        self.refused(lambda: self.fx.read("alpha"), "does not list Alpha/Alpha.xml")

    def test_sums_lists_a_file_not_there(self):
        lines = "".join("%s  %s\n" % (hashlib.sha256(d).hexdigest(), p)
                        for p, d in self.fx.zips["packages/one.zip"].items() if p.startswith("Alpha/"))
        self.fx.sums["packages/one.zip"] = (lines + "%s  Alpha/gone.txt\n" % ("0" * 64)).encode()
        self.fx.write()
        self.refused(lambda: self.fx.read("alpha"), "lists Alpha/gone.txt, which is not in the zip")

    def test_sums_malformed(self):
        self.fx.sums["packages/one.zip"] = b"not a sum line\n"
        self.fx.write()
        self.refused(lambda: self.fx.read("alpha"), "is not a sha256sum line")

    def test_missing_file(self):
        self.change("packages/one.zip", "Alpha/Alpha_ALL_Images.zip", None)
        self.refused(lambda: self.fx.read("alpha"), "lacks Alpha_ALL_Images.zip")

    def test_extra_file(self):
        self.change("packages/two.zip", "Delta/gateway-ip.txt", b"169.254.21.3\n")
        self.refused(lambda: self.fx.read("delta"), "holds gateway-ip.txt, which this script does not know")

    def test_extra_file_in_a_subfolder(self):
        self.change("packages/two.zip", "Delta/extra/thing.bin", b"x")
        self.refused(lambda: self.fx.read("delta"), "holds extra/thing.bin")

    def test_elf(self):
        cases = [
            ("Alpha/libAlpha.so", b"not an elf at all" * 4, "is not an ELF file"),
            ("Alpha/libAlpha.so", elf(m.ET_DYN, bits=2), "32-bit little-endian"),
            ("Alpha/libAlpha.so", elf(m.ET_DYN, order=2), "32-bit little-endian"),
            ("Alpha/libAlpha.so", elf(m.ET_DYN, machine=3), "not built for ARM"),
            ("Alpha/libAlpha.so", elf(m.ET_EXEC), "is not a shared library"),
            ("Alpha/alpha-helper-qnx", elf(m.ET_EXEC, machine=62), "not built for ARM"),
            ("Alpha/alpha-qnx", elf(1), "is not a program"),
        ]
        original = dict(self.fx.zips["packages/one.zip"])
        for path, data, needle in cases:
            with self.subTest(needle=needle, path=path):
                self.fx.zips["packages/one.zip"] = dict(original)
                body = original[path][52:] if path.endswith(".so") else b""
                self.change("packages/one.zip", path, data + body if data[:4] == b"\x7fELF" else data)
                self.refused(lambda: self.fx.read("alpha"), path, needle)

    def test_worker_path(self):
        lib = self.fx.zips["packages/one.zip"]["Alpha/libAlpha.so"]
        self.change("packages/one.zip", "Alpha/libAlpha.so", lib.replace(b"/alpha-helper-qnx\0", b"/other-qnx\0"))
        self.refused(lambda: self.fx.read("alpha"), "does not hold the path %s/Alpha/alpha-helper-qnx" % PLUGINS_ROOT)

    def test_worker_path_put_together(self):
        """The folder and a format with the worker's name after it, both whole strings, will do (HybridDeck)."""
        lib = self.fx.zips["packages/one.zip"]["Alpha/libAlpha.so"]
        folder = ("%s/Alpha/" % PLUGINS_ROOT).encode()
        together = lib.replace(folder + b"alpha-helper-qnx\0", b"x\0") + b"\0" + folder + b"\0%salpha-helper-qnx\0"
        self.change("packages/one.zip", "Alpha/libAlpha.so", together)
        self.fx.read("alpha")
        self.change("packages/one.zip", "Alpha/libAlpha.so", together.replace(b"\0%salpha-helper-qnx\0", b"\0x\0"))
        self.refused(lambda: self.fx.read("alpha"), "does not hold the path %s/Alpha/alpha-helper-qnx" % PLUGINS_ROOT)
        self.change("packages/one.zip", "Alpha/libAlpha.so", together.replace(b"\0" + folder + b"\0", b"\0x\0"))
        self.refused(lambda: self.fx.read("alpha"), "does not hold the path %s/Alpha/alpha-helper-qnx" % PLUGINS_ROOT)

    def test_worker_path_is_whole(self):
        lib = self.fx.zips["packages/one.zip"]["Alpha/libAlpha.so"]
        self.change("packages/one.zip", "Alpha/libAlpha.so", lib.replace(b"/alpha-qnx\0", b"/alpha-qnx2\0"))
        self.refused(lambda: self.fx.read("alpha"), "Alpha/alpha-qnx")

    def with_path(self, path, where="Delta/libDelta.so"):
        data = self.fx.zips["packages/two.zip"][where]
        self.change("packages/two.zip", where, data + b"\x01" + path + b"\0")

    def test_path_strings(self):
        data = (b"\0/var/a/b.prefs\0x=/not/whole\0/one\0 /var/c d/e\0\x7f/dev/null\0/usr/lib/x.so\0"
                b"/var/%s/f\0/unfinished/path")
        self.assertEqual(m.path_strings(data), ["/dev/null", "/usr/lib/x.so", "/var/%s/f", "/var/a/b.prefs"])

    def test_unknown_path_in_the_library(self):
        self.with_path(b"/var/opt/hmi/data/ID5/Delta.prefs")
        self.refused(lambda: self.fx.read("delta"), "Delta/libDelta.so holds the path "
                     "/var/opt/hmi/data/ID5/Delta.prefs, which this packaging does not know", "\"written\"")

    def test_unknown_path_in_a_worker(self):
        self.with_path(b"/tmp/delta/state", "Delta/delta-qnx")
        self.refused(lambda: self.fx.read("delta"), "Delta/delta-qnx holds the path /tmp/delta/state")

    def test_another_plugins_folder_is_unknown(self):
        self.with_path(PLUGINS_ROOT.encode() + b"/Alpha/x")
        self.refused(lambda: self.fx.read("delta"), "holds the path %s/Alpha/x" % PLUGINS_ROOT)
        self.with_path(PLUGINS_ROOT.encode() + b"/DeltaTwo/x")
        self.refused(lambda: self.fx.read("delta"), "holds the path")

    def test_known_paths(self):
        for path in (b"/dev/null", b"/usr/lib/ldqnx.so.2", b"/opt/sys/lib/libx.so.4", b"/lib/libc.so.3", b"/fs/sda0/opt/sys/lib/liby.so.8",
                     PLUGINS_ROOT.encode() + b"/Delta/", PLUGINS_ROOT.encode() + b"/Delta/gateway-ip.txt",
                     b"/var/opt/hmi/data/ID5/gateway-ip.txt", b"/opt/aa/nbt-aad",
                     b"/var/opt/hmi/data/ID5/Plugins/NbtAad/NbtAad.manifest",
                     b"/var/opt/hmi/data/rw/Plugins/CarPlayMenu/librghook.so",
                     b"/opt/mm/lib/libasound/libasound.so", b"/work/build/gcc-4.9.4/libgcc/config/arm/bpabi.S",
                     b"/var/opt/hmi/data/rw/mgflasher-live-profile.txt"):
            self.with_path(path)
            self.with_path(path, "Delta/delta-qnx")
        self.fx.read("delta")

    def test_written_before_path(self):
        self.fx.table["apps"][2]["written_before"] = ["/var/opt/hmi/data/ID5/Delta.old"]
        self.fx.write()
        self.fx.read("delta")
        self.with_path(b"/var/opt/hmi/data/ID5/Delta.old", "Delta/delta-qnx")
        self.refused(lambda: self.fx.read("delta"), "Delta/delta-qnx holds the path /var/opt/hmi/data/ID5/Delta.old, "
                     "which this packaging does not know", "\"written\"")

    def test_written_path(self):
        self.fx.table["apps"][2]["written"] = ["/var/opt/hmi/data/ID5/Delta.prefs"]
        self.fx.write()
        self.refused(lambda: self.fx.read("delta"), "names /var/opt/hmi/data/ID5/Delta.prefs as written by the app "
                     "delta, and Delta/libDelta.so does not hold that path", "\"written_before\"")
        self.with_path(b"/var/opt/hmi/data/ID5/Delta.prefs2")
        self.refused(lambda: self.fx.read("delta"), "holds the path /var/opt/hmi/data/ID5/Delta.prefs2")
        self.change("packages/two.zip", "Delta/libDelta.so", plugin("Delta", ["delta-qnx"], 303)["Delta/libDelta.so"])
        self.with_path(b"/var/opt/hmi/data/ID5/Delta.prefs")
        self.fx.read("delta")

    def test_manifest(self):
        good = plugin_manifest("Alpha", 301)
        cases = [
            (b"<!DOCTYPE plugin>" + good, "document type or entity declaration"),
            (good.replace(b"<plugin>", b"<!DOCTYPE p [<!ENTITY e 'x'>]><plugin>"), "declaration"),
            (good[:-20], "not well-formed XML"),
            (good.replace(b"<name>Alpha</name>", b"<name>Other</name>"), "has name"),
            (good.replace(b"<file>{pluginDir}/Alpha</file>", b"<file>{pluginDir}/Other</file>"), "has file"),
            (good.replace(b"{pluginDir}/Alpha.xml", b"{pluginDir}/Other.xml"), "has DescFile"),
            (good.replace(b"{pluginDir}/Alpha_ALL_Images.zip", b"{pluginDir}/x.zip"), "has ImageDBFile"),
            (good.replace(b"<id>301</id>", b"<id>399</id>"), "claims the connector id(s) 399, not exactly 301"),
            (good.replace(b"</dependencies>", b"<pluginAppConnector><id>302</id></pluginAppConnector>"
                                              b"</dependencies>"), "301, 302"),
            (good.replace(b"<plugin>", b"<plugins>").replace(b"</plugin>", b"</plugins>"), "is not a <plugin>"),
        ]
        for data, needle in cases:
            with self.subTest(needle=needle):
                self.change("packages/one.zip", "Alpha/Alpha.manifest", data)
                self.refused(lambda: self.fx.read("alpha"), needle)

    def test_image_file_named_without_images(self):
        self.change("packages/one.zip", "Bravo/Bravo.manifest", plugin_manifest("Bravo", 302, images=True))
        self.refused(lambda: self.fx.read("bravo"), "names an ImageDBFile")

    def test_description(self):
        cases = [
            (b"<pluginApps><pluginApp entryButtonFocusID='32700'/></pluginApps>", "entryButtonFocusID"),
            (b"<pluginApps><pluginApp>", "not well-formed XML"),
            (b"<!DOCTYPE x><pluginApps/>", "declaration"),
        ]
        for data, needle in cases:
            with self.subTest(needle=needle):
                self.change("packages/one.zip", "Alpha/Alpha.xml", data)
                self.refused(lambda: self.fx.read("alpha"), "Alpha/Alpha.xml", needle)

    def test_image_zip(self):
        self.change("packages/one.zip", "Alpha/Alpha_ALL_Images.zip", b"PK\x03\x04 broken")
        self.refused(lambda: self.fx.read("alpha"), "is not a readable zip")
        damaged = bytearray(images())
        at = damaged.index(b"PNG not really")
        damaged[at] ^= 0xFF
        self.change("packages/one.zip", "Alpha/Alpha_ALL_Images.zip", bytes(damaged))
        self.refused(lambda: self.fx.read("alpha"), "is damaged")


# ---- a zip upstream publishes as a release asset --------------------------------------------------------------

DOWNLOAD = "nbtapps/upstream/up.zip"
DOWNLOAD_URL = "https://github.com/someone/upstream/releases/download/v1.0.0/up.zip"


class _Response(io.BytesIO):
    """What urlopen gives, as far as fetch reads it."""


class DownloadTests(FixtureCase):
    """Delta read from a zip that apps.json pins under downloads, with its plugin folders under payload/."""

    def setUp(self):
        super().setUp()
        members = {"payload/" + p: d for p, d in self.fx.zips["packages/two.zip"].items() if p.startswith("Delta/")}
        members["payload/README.md"] = b"readme\n"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for path, data in members.items():
                z.writestr(path, data)
        self.data = out.getvalue()
        self.fx.table["downloads"] = {DOWNLOAD: {"url": DOWNLOAD_URL, "sha256": hashlib.sha256(self.data).hexdigest(),
                                                 "folder": "payload"}}
        self.fx.table["apps"][2]["zip"] = DOWNLOAD
        self.fx.write()

    def opener(self, data=None):
        calls = []

        def open_url(url):
            calls.append(url)
            return _Response(self.data if data is None else data)
        return open_url, calls

    def test_table(self):
        self.assertIsNone(self.fx.app("alpha")["download"])
        self.assertEqual(self.fx.app("delta")["download"]["folder"], "payload")
        cases = [
            (lambda t: t["downloads"].update({"packages/x.zip": t["downloads"][DOWNLOAD]}), "nbtapps/upstream/"),
            (lambda t: t["downloads"][DOWNLOAD].update(url="http://github.com/a/b/releases/download/v1/up.zip"),
             "is not {url"),
            (lambda t: t["downloads"][DOWNLOAD].update(url="https://example.com/up.zip"), "is not {url"),
            (lambda t: t["downloads"][DOWNLOAD].update(sha256="0" * 63), "is not {url"),
            (lambda t: t["downloads"][DOWNLOAD].update(sha256="A" * 64), "is not {url"),
            (lambda t: t["downloads"][DOWNLOAD].update(folder="../payload"), "is not {url"),
            (lambda t: t["downloads"][DOWNLOAD].pop("folder"), "is not {url"),
            (lambda t: t.update(downloads=[]), "downloads is not an object"),
            (lambda t: t["apps"][0].update(zip="nbtapps/upstream/other.zip"), "a path in downloads"),
        ]
        for mutate, needle in cases:
            with self.subTest(needle=needle):
                table = copy.deepcopy(self.fx.table)
                mutate(table)
                self.fx.put(m.TABLE, json.dumps(table, indent=2).encode())
                self.refused(self.fx.table_now, needle)

    def test_not_fetched_yet(self):
        self.refused(lambda: self.fx.read("delta"), DOWNLOAD + " is not there", "make_nbtpkg.py fetch", DOWNLOAD_URL)

    def test_read_from_the_pinned_zip(self):
        self.fx.put(DOWNLOAD, self.data)
        files, note = m.read_plugin(self.fx.root, self.fx.app("delta"))
        self.assertEqual(files, {p[len("Delta/"):]: d for p, d in self.fx.zips["packages/two.zip"].items()
                                 if p.startswith("Delta/")})
        self.assertIn("the zip's SHA-256 is the one apps.json pins", note)
        self.fx.read("delta")

    def test_another_zip_at_the_path(self):
        self.fx.put(DOWNLOAD, self.data + b"\0")
        self.refused(lambda: self.fx.read("delta"), "not %s as apps.json pins" % hashlib.sha256(self.data).hexdigest())

    def test_folder_checked_as_in_packages(self):
        with zipfile.ZipFile(io.BytesIO(self.data)) as z:
            members = {i.filename: z.read(i) for i in z.infolist()}
        members["payload/Delta/extra.bin"] = b"x"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for path, data in members.items():
                z.writestr(path, data)
        self.data = out.getvalue()
        self.fx.table["downloads"][DOWNLOAD]["sha256"] = hashlib.sha256(self.data).hexdigest()
        self.fx.write()
        self.fx.put(DOWNLOAD, self.data)
        self.refused(lambda: self.fx.read("delta"), "the folder payload/Delta/ holds extra.bin")

    def test_fetch(self):
        open_url, calls = self.opener()
        lines = []
        m.fetch(self.fx.root, self.fx.table_now(), opener=open_url, log=lines.append)
        self.assertEqual(calls, [DOWNLOAD_URL])
        with open(self.fx.path(*DOWNLOAD.split("/")), "rb") as f:
            self.assertEqual(f.read(), self.data)
        self.assertFalse(os.path.exists(self.fx.path(*DOWNLOAD.split("/")) + ".part"))
        self.fx.read("delta")
        # Once it is there, it is checked and not downloaded again.
        m.fetch(self.fx.root, self.fx.table_now(), opener=open_url, log=lines.append)
        self.assertEqual(calls, [DOWNLOAD_URL])
        self.assertIn("there, SHA-256", lines[-1])

    def test_fetch_checks_a_file_there(self):
        self.fx.put(DOWNLOAD, b"something else")
        open_url, calls = self.opener()
        self.refused(lambda: m.fetch(self.fx.root, self.fx.table_now(), opener=open_url, log=QUIET),
                     "remove it and run")
        self.assertEqual(calls, [])

    def test_fetch_another_file_writes_nothing(self):
        open_url, _ = self.opener(b"not the zip")
        self.refused(lambda: m.fetch(self.fx.root, self.fx.table_now(), opener=open_url, log=QUIET),
                     "nothing was written")
        self.assertFalse(os.path.exists(self.fx.path("nbtapps", "upstream")))

    def test_fetch_too_large(self):
        open_url, _ = self.opener(b"x" * 100)
        with mock_attr(m, "MAX_DOWNLOAD", 10):
            self.refused(lambda: m.fetch(self.fx.root, self.fx.table_now(), opener=open_url, log=QUIET),
                         "is larger than 10 bytes")
        self.assertFalse(os.path.exists(self.fx.path("nbtapps", "upstream")))

    def test_fetch_fails_to_download(self):
        def open_url(url):
            raise OSError("no network")
        self.refused(lambda: m.fetch(self.fx.root, self.fx.table_now(), opener=open_url, log=QUIET),
                     "cannot be downloaded: no network")

    def test_fetch_with_no_downloads(self):
        del self.fx.table["downloads"]
        self.fx.table["apps"][2]["zip"] = "packages/two.zip"
        self.fx.write()
        open_url, calls = self.opener()
        m.fetch(self.fx.root, self.fx.table_now(), opener=open_url, log=QUIET)
        self.assertEqual(calls, [])


@contextlib.contextmanager
def mock_attr(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


# ---- the manifest and the staged folder -----------------------------------------------------------------------

class ManifestTests(FixtureCase):
    def test_without_layouts(self):
        table = self.fx.table_now()
        manifest = m.make_manifest(table, table["apps"]["alpha"], "1.2.3")
        self.assertEqual(manifest, {
            "manifestVersion": 1, "id": "com.example.test.alpha", "name": "Alpha", "version": "1.2.3",
            "requires": {"agent": ">=1.6.0"},
            "install": {"files": [
                {"src": "payload/Alpha/", "dest": "${var_plugins}/Alpha/", "mode": "0644"},
                {"src": "payload/bin/alpha-qnx", "dest": "${var_plugins}/Alpha/alpha-qnx", "mode": "0755"},
                {"src": "payload/bin/alpha-helper-qnx", "dest": "${var_plugins}/Alpha/alpha-helper-qnx",
                 "mode": "0755"}],
                "reboot": "hmi-restart"}})

    def test_with_layouts_and_conflicts(self):
        table = self.fx.table_now()
        manifest = m.make_manifest(table, table["apps"]["delta"], "2.0.0-beta.1")
        self.assertEqual(manifest["conflicts"], [{"id": "com.example.test.alpha"}])
        self.assertEqual(manifest["install"]["hooks"], {
            "postInstall": {"script": "hooks/select-layout.sh", "timeout": 30},
            "preRemove": {"script": "hooks/remove-layout.sh", "timeout": 30}})
        self.assertEqual(manifest["install"]["reboot"], "hmi-restart")
        self.assertNotIn("conflicts", m.make_manifest(table, table["apps"]["bravo"], "1.0.0"))

    def test_stage_without_layouts(self):
        table = self.fx.table_now()
        staged, _ = m.stage(self.fx.root, table, table["apps"]["alpha"], "1.0.0", self.fx.read("alpha"))
        self.assertEqual(sorted(staged), sorted([
            "payload/Alpha/libAlpha.so", "payload/Alpha/Alpha.manifest", "payload/Alpha/Alpha.xml",
            "payload/Alpha/Alpha_ALL_Images.zip", "payload/bin/alpha-qnx", "payload/bin/alpha-helper-qnx",
            "licenses/LICENSE", "licenses/T-LICENSE"]))
        self.assertEqual(staged["licenses/LICENSE"], self.fx.files["source/LICENSE"])
        staged, _ = m.stage(self.fx.root, table, table["apps"]["bravo"], "1.0.0", self.fx.read("bravo"))
        self.assertEqual(sorted(staged), sorted([
            "payload/Bravo/libBravo.so", "payload/Bravo/Bravo.manifest", "payload/Bravo/Bravo.xml",
            "payload/bin/bravo-qnx", "licenses/LICENSE"]))

    def test_without_layouts_saved_before(self):
        """An app that no longer saves a file still has the hook, so that a removal takes away what an earlier
        version left; with `written` too, the hook removes both, `written` first."""
        self.fx.put("%s/remove-saved.sh.in" % m.HOOKS, b"written=\"@WRITTEN@\"\n")
        self.fx.table["apps"][1]["written_before"] = [PLUGINS_ROOT + "/Bravo/bravo-on"]
        self.fx.write()
        table = self.fx.table_now()
        app = table["apps"]["bravo"]
        self.assertEqual(m.hook_templates(app), (("preRemove", "remove-saved.sh"),))
        self.assertEqual(m.stamp_hooks(self.fx.root, table, app),
                         {"remove-saved.sh": ("written=\"%s/Bravo/bravo-on\"\n" % PLUGINS_ROOT).encode()})
        self.fx.read("bravo")
        app = dict(app, written=["/var/a/Bravo.state"])
        self.assertEqual(m.saved_files(app), ["/var/a/Bravo.state", PLUGINS_ROOT + "/Bravo/bravo-on"])

    def test_without_layouts_saving(self):
        saved = self.saving_alpha()
        table = self.fx.table_now()
        app = table["apps"]["alpha"]
        self.assertEqual(m.hook_templates(app), (("preRemove", "remove-saved.sh"),))
        self.assertEqual(m.hook_templates(table["apps"]["bravo"]), ())
        self.assertEqual(m.make_manifest(table, app, "1.0.0")["install"]["hooks"],
                         {"preRemove": {"script": "hooks/remove-saved.sh", "timeout": 30}})
        self.assertNotIn("hooks", m.make_manifest(table, table["apps"]["bravo"], "1.0.0")["install"])
        hooks = m.stamp_hooks(self.fx.root, table, app)
        self.assertEqual(hooks, {"remove-saved.sh": ("#!/bin/sh\n# Alpha\nwritten=\"%s\"\n" % " ".join(saved)).encode()})
        staged, _ = m.stage(self.fx.root, table, app, "1.0.0", self.fx.read("alpha"), hooks=hooks)
        self.assertEqual(staged["hooks/remove-saved.sh"], hooks["remove-saved.sh"])
        self.assertIn("payload/Alpha/Alpha.xml", staged)
        self.refused(lambda: m.stage(self.fx.root, table, app, "1.0.0", self.fx.read("alpha")),
                     "the app alpha has the hooks remove-saved.sh, and was given none")
        self.refused(lambda: m.stage(self.fx.root, table, table["apps"]["bravo"], "1.0.0", self.fx.read("bravo"),
                                     hooks=hooks), "the app bravo has the hooks none, and was given remove-saved.sh")

    def test_saving_template_missing(self):
        self.saving_alpha(template=False)
        table = self.fx.table_now()
        self.refused(lambda: m.stamp_hooks(self.fx.root, table, table["apps"]["alpha"]),
                     "the hook template nbtapps/hooks/remove-saved.sh.in is not there")

    def test_licence_missing(self):
        os.remove(self.fx.path("source", "third-party", "T-LICENSE"))
        table = self.fx.table_now()
        self.refused(lambda: m.stage(self.fx.root, table, table["apps"]["alpha"], "1.0.0", self.fx.read("alpha")),
                     "source/third-party/T-LICENSE is not there")


# ---- layouts and hooks ----------------------------------------------------------------------------------------

class LayoutTests(FixtureCase):
    def layouts(self):
        table = self.fx.table_now()
        app = table["apps"]["delta"]
        files = self.fx.read("delta")
        return m.read_layouts(self.fx.root, table, app, files), m.stamp_hooks(self.fx.root, table, app)

    def test_nothing_there_yet(self):
        message = self.refused(self.layouts, "cannot be built yet", "from the layout step", "from the hook step",
                               "nbtapps/layouts/Delta/SOURCE", "nbtapps/layouts/Delta/800x480/Delta.xml",
                               "nbtapps/layouts/Delta/1920x720/Delta_ALL_Images.zip",
                               "nbtapps/hooks/select-layout.sh.in", "nbtapps/hooks/remove-layout.sh.in")
        self.assertNotIn("1280x480/Delta.xml", message)   # the base size comes from the zip

    def test_a_size_missing(self):
        self.fx.add_layouts(sizes=["800x480"])
        self.fx.add_hooks()
        message = self.refused(self.layouts, "nbtapps/layouts/Delta/1920x720/Delta.xml")
        self.assertNotIn("from the hook step", message)
        self.assertNotIn("800x480/Delta.xml", message)

    def test_hooks_missing(self):
        self.fx.add_layouts()
        message = self.refused(self.layouts, "from the hook step", "select-layout.sh.in")
        self.assertNotIn("from the layout step", message)

    def test_stale_source(self):
        self.fx.add_layouts()
        self.fx.add_hooks()
        self.change("packages/two.zip", "Delta/Delta.xml", description("new upstream"))
        self.refused(self.layouts, "are stale", "Delta/Delta.xml")

    def test_source_names_other_files(self):
        self.fx.add_layouts(source=("%s  Delta/Delta.xml\n" % ("0" * 64)).encode())
        self.fx.add_hooks()
        self.refused(self.layouts, "SOURCE names Delta/Delta.xml; it names exactly")

    def test_stray_size(self):
        self.fx.add_layouts(sizes=["800x480", "1920x720", "1024x600"])
        self.fx.add_hooks()
        self.refused(self.layouts, "holds 1024x600, which is no size in apps.json")

    def test_stray_file_in_a_size(self):
        self.fx.add_layouts()
        self.fx.add_hooks()
        self.fx.put("%s/Delta/800x480/notes.txt" % m.LAYOUTS, b"x")
        self.refused(self.layouts, "holds notes.txt")

    def test_generated_layout_checked(self):
        self.fx.add_layouts()
        self.fx.add_hooks()
        self.fx.put("%s/Delta/800x480/Delta.xml" % m.LAYOUTS, b"<pluginApp entryButtonFocusID='1'/>")
        self.refused(self.layouts, "800x480/Delta.xml", "entryButtonFocusID")

    def test_leftover_placeholder(self):
        self.fx.add_layouts()
        self.fx.add_hooks(select=SELECT_HOOK + "echo @ROTATION@\n")
        self.refused(self.layouts, "select-layout.sh.in has the placeholder(s) @ROTATION@")

    def test_happy_path(self):
        self.fx.add_layouts()
        self.fx.add_hooks()
        layouts, hooks = self.layouts()
        self.assertEqual(list(layouts), ["1280x480", "800x480", "1920x720"])
        self.assertEqual(layouts["1280x480"]["Delta.xml"], self.fx.zips["packages/two.zip"]["Delta/Delta.xml"])
        self.assertEqual(hooks["select-layout.sh"].decode(), "#!/bin/sh\n# picks a layout of Delta (default 1280x480)"
                                                             "\ncase \"$size\" in 1280x480|800x480|1920x720) ;; esac\n")
        self.assertEqual(hooks["remove-layout.sh"].decode(),
                         "#!/bin/sh\nrm -f \"$NBT_VAR_PLUGINS_DIR/Delta/Delta.xml\"\n")
        table = self.fx.table_now()
        staged, _ = m.stage(self.fx.root, table, table["apps"]["delta"], "1.0.0", self.fx.read("delta"),
                            layouts, hooks)
        expected = ["payload/Delta/libDelta.so", "payload/Delta/Delta.manifest", "payload/bin/delta-qnx",
                    "hooks/select-layout.sh", "hooks/remove-layout.sh", "licenses/LICENSE"]
        for size in ("1280x480", "800x480", "1920x720"):
            expected += ["payload/Delta/layouts/%s/Delta.xml" % size,
                         "payload/Delta/layouts/%s/Delta_ALL_Images.zip" % size]
        self.assertEqual(sorted(staged), sorted(expected))   # nothing at payload/Delta/Delta.xml: the hook makes it

    def test_base_layout_at_and_written(self):
        self.fx.table["apps"][2].update(base_layout_at=["1920x720"], written=["/var/a/Delta.prefs", "/var/b/d.x"])
        lib = self.fx.zips["packages/two.zip"]["Delta/libDelta.so"]
        self.fx.zips["packages/two.zip"]["Delta/libDelta.so"] = lib + b"\0/var/a/Delta.prefs\0/var/b/d.x\0"
        self.fx.write()
        self.fx.add_layouts()   # the layout of every generated size is still there to be read and checked
        self.fx.add_hooks(select=SELECT_HOOK + "case $size in @BASE_AT@) ;; esac\n",
                          remove=REMOVE_HOOK + "written=\"@WRITTEN@\"\n")
        layouts, hooks = self.layouts()
        self.assertEqual(list(layouts), ["1280x480", "800x480", "1920x720"])
        self.assertTrue(hooks["select-layout.sh"].decode().endswith(
            "case \"$size\" in 1280x480|800x480) ;; esac\ncase $size in 1920x720) ;; esac\n"))
        self.assertTrue(hooks["remove-layout.sh"].decode().endswith("written=\"/var/a/Delta.prefs /var/b/d.x\"\n"))
        table = self.fx.table_now()
        staged, _ = m.stage(self.fx.root, table, table["apps"]["delta"], "1.0.0", self.fx.read("delta"),
                            layouts, hooks)
        self.assertEqual(sorted(p for p in staged if "/layouts/" in p),
                         ["payload/Delta/layouts/1280x480/Delta.xml", "payload/Delta/layouts/1280x480/Delta_ALL_Images.zip",
                          "payload/Delta/layouts/800x480/Delta.xml", "payload/Delta/layouts/800x480/Delta_ALL_Images.zip"])

    def test_no_overrides_stamp_harmless_values(self):
        self.fx.add_layouts()
        self.fx.add_hooks(select="case $size in @BASE_AT@) ;; esac\n", remove="written=\"@WRITTEN@\"\n")
        _, hooks = self.layouts()
        self.assertEqual(hooks["select-layout.sh"], b"case $size in none) ;; esac\n")
        self.assertEqual(hooks["remove-layout.sh"], b"written=\"\"\n")

    @needs_nbtpkg
    def test_happy_path_builds(self):
        self.fx.add_layouts()
        self.fx.add_hooks()
        result = m.build(self.fx.root, self.fx.table_now(), "delta", "1.0.0", self.out, NBTPKG, log=QUIET)
        with open(result["archive"], "rb") as f:
            info = NBTPKG.verify_package(f.read(), allow_unsigned=True)
        self.assertEqual(info["flags"], ["runs-scripts", "needs-reboot"])
        self.assertEqual(result["manifest"]["conflicts"], [{"id": "com.example.test.alpha"}])

    @needs_nbtpkg
    def test_without_layouts_saving_builds(self):
        self.saving_alpha()
        result = m.build(self.fx.root, self.fx.table_now(), "alpha", "1.0.0", self.out, NBTPKG, log=QUIET)
        with open(result["archive"], "rb") as f:
            data = f.read()
        info = NBTPKG.verify_package(data, allow_unsigned=True)
        self.assertEqual(info["flags"], ["runs-scripts", "needs-reboot"])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn("hooks/remove-saved.sh", z.namelist())
            self.assertNotIn("hooks/select-layout.sh", z.namelist())


# ---- arguments and the record ---------------------------------------------------------------------------------

class ArgumentTests(unittest.TestCase):
    def test_versions(self):
        for good in ("1.0.0", "0.0.1", "10.20.30", "1.2.3-beta.1", "1.2.3-rc-1", "1.0.0-0", "1.0.0-x.7.z.92"):
            self.assertEqual(m.check_version(good), good)
        for bad in ("1.0", "1.0.0.0", "v1.0.0", "01.0.0", "1.0.0-01", "1.0.0-", "1.0.0-a..b", "1.0.0+build.1",
                    "1.0.0-x.lock", "1.0.0\n", " 1.0.0", "1.0.0-a b", "1.0.0-ä", "1.0.0/x", "1.0.0-" + "a" * 60):
            with self.subTest(bad=bad):
                self.assertRaises(m.Refused, m.check_version, bad)

    def test_repos(self):
        for good in ("NBTApps/ID6_apps_prerelease", "a/b", "a-b/c.d_e"):
            self.assertEqual(m.check_repo(good), good)
        for bad in ("a", "a/b/c", "-a/b", "a/..", "a/.", "a b/c", "a/b?x", "a/b#x", "a/b.git", "a/b\n", ""):
            with self.subTest(bad=bad):
                self.assertRaises(m.Refused, m.check_repo, bad)

    def test_dates(self):
        self.assertEqual(m.check_released("2026-11-18"), "2026-11-18")
        for bad in ("2026-13-01", "2026-02-30", "2026-1-1", "2026-11-18\n", "18-11-2026", "2026-11-18T00:00"):
            with self.subTest(bad=bad):
                self.assertRaises(m.Refused, m.check_released, bad)

    def test_notes(self):
        self.assertEqual(m.check_notes(["First release."]), ["First release."])
        for bad in ("", "   ", "a\nb", "tab\there", "x" * 201):
            with self.subTest(bad=bad):
                self.assertRaises(m.Refused, m.check_notes, [bad])

    def test_app_unknown(self):
        table = m.load_table(REPO_ROOT)
        with self.assertRaises(m.Refused):
            m.build(REPO_ROOT, table, "no-such-app", "1.0.0", "/nonexistent", None, log=QUIET)


class RecordTests(unittest.TestCase):
    APP = {"key": "road-jet", "id": "com.github.nbtapps.road-jet"}

    def test_record(self):
        record = m.make_record(self.APP, "1.2.0-beta.1", "ab" * 32, "NBTApps/ID6_apps_prerelease", "c" * 40,
                               "2026-11-18", ["One.", "Two."])
        self.assertEqual(record, {"id": "com.github.nbtapps.road-jet", "version": "1.2.0-beta.1", "record": {
            "url": "https://github.com/NBTApps/ID6_apps_prerelease/releases/download/road-jet-v1.2.0-beta.1/"
                   "com.github.nbtapps.road-jet-1.2.0-beta.1.nbtpkg",
            "sha256": "ab" * 32, "released": "2026-11-18", "notes": ["One.", "Two."],
            "source": {"repo": "https://github.com/NBTApps/ID6_apps_prerelease", "commit": "c" * 40}}})

    def test_no_notes_no_channel(self):
        record = m.make_record(self.APP, "1.0.0", "ab" * 32, "o/r", "c" * 40, "2026-11-18", [])["record"]
        self.assertEqual(sorted(record), ["released", "sha256", "source", "url"])


# ---- the working tree, keys and whole builds ------------------------------------------------------------------

@needs_git
class TreeTests(FixtureCase):
    def test_clean_and_dirty(self):
        self.fx.put("nbtapps/.gitignore", b"/out/\n")
        commit = self.fx.commit_all()
        self.assertEqual(m.tree_state(self.fx.root), (commit, []))
        self.fx.put("notes.txt", b"outside the watched folders\n")
        self.fx.put("nbtapps/out/x.nbtpkg", b"ignored\n")
        self.assertEqual(m.tree_state(self.fx.root), (commit, []))
        self.fx.put("nbtapps/layouts/Delta/SOURCE", b"untracked\n")
        self.fx.put("source/LICENSE", b"changed\n")
        self.assertEqual(m.tree_state(self.fx.root)[1], ["nbtapps/layouts/Delta/SOURCE", "source/LICENSE"])

    def test_not_a_repo(self):
        self.refused(lambda: m.tree_state(self.fx.root), "git rev-parse")


@needs_keys
class KeyTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.keys = tempfile.mkdtemp(prefix="make-nbtpkg-keys-")
        self.addCleanup(shutil.rmtree, self.keys, True)
        self.seed, self.public, self.key_file, self.pub_file = make_keys(self.keys)

    def test_from_file_and_environment(self):
        self.assertEqual(m.resolve_key(NBTPKG, self.key_file, {}, False), (self.seed, "--key"))
        self.assertEqual(m.resolve_key(NBTPKG, None, {m.KEY_ENV: self.seed.hex() + "\n"}, True),
                         (self.seed, m.KEY_ENV))
        self.assertEqual(m.resolve_key(NBTPKG, None, {}, False), (None, None))

    def test_refusals(self):
        self.refused(lambda: m.resolve_key(NBTPKG, None, {}, True), "--require-key")
        self.refused(lambda: m.resolve_key(NBTPKG, self.key_file, {m.KEY_ENV: self.seed.hex()}, False), "both")
        secret = "zz" + self.seed.hex()[2:]
        message = self.refused(lambda: m.resolve_key(NBTPKG, None, {m.KEY_ENV: secret}, False), "64 hex")
        self.assertNotIn(secret, message)
        bad = os.path.join(self.keys, "bad.key")
        with open(bad, "w") as f:
            f.write("not a key\n")
        self.refused(lambda: m.resolve_key(NBTPKG, bad, {}, False), "is not a key file")
        self.refused(lambda: m.resolve_key(NBTPKG, bad + ".none", {}, False), "is not there")

    def test_wrong_public_key(self):
        other = NBTPKG.public_key(os.urandom(32))
        self.refused(lambda: m.check_pair(NBTPKG, self.seed, other), "not the private half")
        self.refused(lambda: m.build(self.fx.root, self.fx.table_now(), "alpha", "1.0.0", self.out, NBTPKG,
                                     seed=self.seed, pub=other, log=QUIET), "not the private half")
        self.assertEqual(os.listdir(self.out), [])

    def test_signed_clean_tree_writes_the_record(self):
        commit = self.fx.commit_all()
        result = m.build(self.fx.root, self.fx.table_now(), "alpha", "1.0.0", self.out, NBTPKG, seed=self.seed,
                         pub=self.public, released="2026-11-18", notes=["First."], repo="Some-One/repo",
                         log=QUIET)
        with open(result["archive"], "rb") as f:
            data = f.read()
        info = NBTPKG.verify_package(data, keys=[self.public])
        self.assertEqual(info["key"], self.public.hex())
        with open(result["record"]) as f:
            record = json.load(f)
        self.assertEqual(record, m.make_record(self.fx.app("alpha"), "1.0.0", hashlib.sha256(data).hexdigest(),
                                               "Some-One/repo", commit, "2026-11-18", ["First."]))
        self.assertEqual(sorted(os.listdir(self.out)), ["com.example.test.alpha-1.0.0.nbtpkg", m.RECORD])

    def test_signed_dirty_tree(self):
        self.fx.commit_all()
        self.fx.put("nbtapps/apps.json", json.dumps(dict(self.fx.table, requires_agent=">=1.7.0")).encode())
        table = self.fx.table_now()
        self.refused(lambda: m.build(self.fx.root, table, "alpha", "1.0.0", self.out, NBTPKG, seed=self.seed,
                                     pub=self.public, log=QUIET), "not committed", "nbtapps/apps.json")
        self.assertEqual(os.listdir(self.out), [])
        lines = []
        result = m.build(self.fx.root, table, "alpha", "1.0.0", self.out, NBTPKG, seed=self.seed, pub=self.public,
                         allow_dirty=True, log=lines.append)
        self.assertIsNone(result["record"])
        self.assertTrue(any("--allow-dirty" in line and "no nbtpkg-record.json" in line for line in lines))
        self.assertEqual(os.listdir(self.out), ["com.example.test.alpha-1.0.0.nbtpkg"])
        with open(result["archive"], "rb") as f:
            NBTPKG.verify_package(f.read(), keys=[self.public])

    def test_unsigned_build_has_no_record(self):
        self.fx.commit_all()
        result = m.build(self.fx.root, self.fx.table_now(), "bravo", "1.0.0", self.out, NBTPKG, log=QUIET)
        self.assertIsNone(result["record"])
        with open(result["archive"], "rb") as f:
            data = f.read()
        self.assertRaises(NBTPKG.PkgError, NBTPKG.verify_package, data)
        NBTPKG.verify_package(data, allow_unsigned=True)

    def test_command_line_key_from_environment(self):
        """The key in the environment reaches neither the output nor a file."""
        self.fx.commit_all()
        self.fx.put("nbtapps/author.pub", open(self.pub_file, "rb").read())
        self.fx.git("add", "-A")
        self.fx.git("commit", "-q", "-m", "pub")
        out, err = io.StringIO(), io.StringIO()
        environ = {m.KEY_ENV: self.seed.hex(), m.HEADUNIT_ENV: HEADUNIT}
        with mock_environ(environ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.main(["build", "--app", "alpha", "--version", "1.0.0", "--out", self.out, "--require-key"],
                   root=self.fx.root)
        self.assertIn("signing with the key from %s" % m.KEY_ENV, out.getvalue())
        self.assertNotIn(self.seed.hex(), out.getvalue() + err.getvalue())
        for name in os.listdir(self.out):
            with open(os.path.join(self.out, name), "rb") as f:
                self.assertNotIn(self.seed.hex().encode(), f.read())
        self.assertEqual(sorted(os.listdir(self.out)), ["com.example.test.alpha-1.0.0.nbtpkg", m.RECORD])

    def test_command_line_without_author_pub(self):
        with mock_environ({m.KEY_ENV: self.seed.hex(), m.HEADUNIT_ENV: HEADUNIT}):
            with self.assertRaises(SystemExit) as caught:
                m.main(["build", "--app", "alpha", "--version", "1.0.0", "--out", self.out], root=self.fx.root)
        self.assertIn("nbtapps/author.pub", str(caught.exception))
        self.assertNotIn(self.seed.hex(), str(caught.exception))


@contextlib.contextmanager
def mock_environ(values):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@needs_nbtpkg
class BuildTests(FixtureCase):
    def test_uninstalled_files_are_allowed(self):
        """licenses/ rides in the archive and nothing installs it: nbtpkg.py takes that."""
        result = m.build(self.fx.root, self.fx.table_now(), "alpha", "1.0.0", self.out, NBTPKG, log=QUIET)
        manifest = result["manifest"]
        paths = [c["path"] for c in manifest["contents"]]
        self.assertIn("licenses/LICENSE", paths)
        self.assertIn("licenses/T-LICENSE", paths)
        self.assertFalse(any(f["src"].startswith("licenses") for f in manifest["install"]["files"]))
        with open(result["archive"], "rb") as f:
            NBTPKG.verify_package(f.read(), allow_unsigned=True)

    def test_same_input_same_bytes(self):
        first = m.build(self.fx.root, self.fx.table_now(), "alpha", "1.0.0", self.out, NBTPKG, log=QUIET)
        with open(first["archive"], "rb") as f:
            one = f.read()
        os.remove(first["archive"])
        second = m.build(self.fx.root, self.fx.table_now(), "alpha", "1.0.0", self.out, NBTPKG, log=QUIET)
        with open(second["archive"], "rb") as f:
            self.assertEqual(f.read(), one)

    def test_layout_app_without_layouts(self):
        self.refused(lambda: m.build(self.fx.root, self.fx.table_now(), "delta", "1.0.0", self.out, NBTPKG,
                                     log=QUIET), "cannot be built yet")
        self.assertEqual(os.listdir(self.out), [])


# ---- the real zips --------------------------------------------------------------------------------------------

class RealZipTests(unittest.TestCase):
    """Upstream's zips in this working tree, as they are."""

    def test_every_app_passes_the_checks(self):
        table = m.load_table(REPO_ROOT)
        for key, app in table["apps"].items():
            with self.subTest(key=key):
                files, _ = m.read_plugin(REPO_ROOT, app)
                m.check_plugin(table, app, files)

    def test_layout_apps_wait_for_their_steps(self):
        table = m.load_table(REPO_ROOT)
        for key, app in table["apps"].items():
            if not app["layouts"]:
                continue
            layouts, hooks = m.layout_needs(table, app)
            if all(os.path.isfile(os.path.join(REPO_ROOT, p)) for p in layouts + hooks):
                continue   # the later steps are in: the build tests below cover it
            with self.subTest(key=key), self.assertRaises(m.Refused) as caught:
                m.build(REPO_ROOT, table, key, "1.0.0", "/nonexistent", None, log=QUIET)
            self.assertIn("cannot be built yet", str(caught.exception))

    @needs_keys
    def test_end_to_end(self):
        """The apps that need no layout step, from the real zips, signed with a throwaway key, built twice."""
        table = m.load_table(REPO_ROOT)
        nbtcat = load_nbtcat()
        work = tempfile.mkdtemp(prefix="make-nbtpkg-e2e-")
        self.addCleanup(shutil.rmtree, work, True)
        seed, public, _, _ = make_keys(work)
        for key, app in table["apps"].items():
            if app["layouts"]:
                continue
            with self.subTest(key=key):
                archives = []
                for run in ("a", "b"):
                    out = os.path.join(work, run)
                    result = m.build(REPO_ROOT, table, key, "1.0.0", out, NBTPKG, seed=seed, pub=public,
                                     allow_dirty=True, log=QUIET)
                    with open(result["archive"], "rb") as f:
                        archives.append(f.read())
                self.assertEqual(archives[0], archives[1])
                data = archives[0]
                info = NBTPKG.verify_package(data, keys=[public])
                self.assertEqual((info["id"], info["version"]), (app["id"], "1.0.0"))
                hooks = [script for _, script in m.hook_templates(app)]
                self.assertEqual(info["flags"], (["runs-scripts"] if hooks else []) + ["needs-reboot"])
                self.assertEqual(hooks, [])
                placed = m.make_manifest(table, app, "1.0.0")["install"]["files"]
                self.assertEqual(result["manifest"]["install"]["files"], placed)
                names = {c["path"] for c in result["manifest"]["contents"]}
                expected = {"payload/%s/%s" % (app["folder"], n) for n in m.plugin_files(app)
                            if n not in app["workers"]}
                expected |= {"payload/bin/" + w for w in app["workers"]}
                expected |= {"licenses/" + os.path.basename(p) for p in [m.PROJECT_LICENSE] + app.get("licenses", [])}
                expected |= {"hooks/" + script for script in hooks}
                self.assertEqual(names, expected)
                if nbtcat is not None:
                    facts, problems = nbtcat.inspect_archive(app["id"], "1.0.0", data, [public])
                    self.assertEqual(problems, [])
                    self.assertEqual(facts["ids"], {"connector": {app["connector"]}, "focus": set()})


if __name__ == "__main__":
    unittest.main()
