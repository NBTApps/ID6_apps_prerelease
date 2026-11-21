#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Repackages upstream's prebuilt iDrive 6 plugins as NBTApps packages (.nbtpkg), one package per app.

  make_nbtpkg.py list
  make_nbtpkg.py build --app KEY --version X.Y.Z [--out DIR] [--key FILE] [--require-key] [--pub FILE]
                       [--released YYYY-MM-DD] [--note TEXT]... [--repo OWNER/REPO] [--allow-dirty]
                       [--nbtapps DIR]

Nothing is compiled. `build` reads one plugin folder out of its zip in packages/, checks it, stages a package
source folder and has NBTApps' tools/nbtpkg.py build the archive. --nbtapps (or the environment variable
NBTAPPS_HEADUNIT) is the headunit/ folder of an NBTApps checkout. nbtpkg.py is loaded from there and not copied
here, as it is under another licence.

The apps are in apps.json beside this file. README.md beside it has the package layout, the contract with the
layout and hook steps, and how a release is made.

Signing: the private key comes from --key FILE (a key file as nbtpkg.py keygen writes it) or from the environment
variable NBTPKG_AUTHOR_KEY (64 hex characters), which is never written to disk or printed. A signed build checks
that the key is the private half of nbtapps/author.pub (or --pub) before it builds, and verifies the archive with
that public key after. --require-key makes a build with no key an error.

A signed build from a clean tree also writes nbtpkg-record.json beside the archive, for the nbtapps-packages bot.
Its `source.commit` is HEAD, so a signed build refuses a tree with changes under packages/, source/ or nbtapps/
that are not committed. With --allow-dirty it builds and signs such a tree, and writes no record.

Needs python3 (standard library only) and, to sign or check a signature, the `cryptography` module that nbtpkg.py
uses.
"""
import argparse
import datetime
import hashlib
import importlib.util
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ElementTree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # the repo's working tree

TABLE = "nbtapps/apps.json"
AUTHOR_PUB = "nbtapps/author.pub"
LAYOUTS = "nbtapps/layouts"
HOOKS = "nbtapps/hooks"
PROJECT_LICENSE = "source/LICENSE"
WATCHED = ("packages", "source", "nbtapps")  # a signed build's record names HEAD, so these must be committed

KEY_ENV = "NBTPKG_AUTHOR_KEY"
HEADUNIT_ENV = "NBTAPPS_HEADUNIT"
DEFAULT_REPO = "NBTApps/ID6_apps_prerelease"
RECORD = "nbtpkg-record.json"
HOOK_TIMEOUT = 30
HOOK_TEMPLATES = (("postInstall", "select-layout.sh"), ("preRemove", "remove-layout.sh"))
SUMS = "SHA256SUMS"
MAX_MEMBER = 64 << 20                        # no plugin file comes near this

# What the version, the repo and the date may be: they end up in a tag, a URL and a file name.
_NUMBER = r"(?:0|[1-9][0-9]{0,8})"
_PRE_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"     # semver: no leading zero in a number
VERSION = re.compile(r"%s\.%s\.%s(?:-%s(?:\.%s)*)?" % (_NUMBER, _NUMBER, _NUMBER, _PRE_ID, _PRE_ID))
REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NOTE = re.compile(r"[^\x00-\x1f\x7f]*\S[^\x00-\x1f\x7f]*")    # as schema/version.schema.json has it
KEY_HEX = re.compile(r"[0-9a-fA-F]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")

_LABEL = r"[a-z0-9]+(?:-[a-z0-9]+)*"
APP_KEY = re.compile(_LABEL)
PACKAGE_ID = re.compile(r"%s(?:\.%s)+" % (_LABEL, _LABEL))
FOLDER = re.compile(r"[A-Za-z0-9_]{1,64}")
WORKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}-qnx")
SIZE = re.compile(r"[1-9][0-9]{2,4}x[1-9][0-9]{2,4}")
ZIP_PATH = re.compile(r"packages/[A-Za-z0-9._-]+\.zip")
REL_PATH = re.compile(r"(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")
ABS_FILE = re.compile(r"(?:/[A-Za-z0-9._-]+)+")   # a `written` path; it goes into a shell script as it is
# An absolute path as a whole string of a binary: after a byte that is no printable character, up to a NUL, with a
# second slash in it. Paths a program builds at run time, or keeps inside a longer string, are not seen.
PATH_STRING = re.compile(rb"(?<![\x21-\x7e])/[\x21-\x7e]+/[\x21-\x7e]*(?=\x00)")
# Where a plugin's files may name paths without a word here: the null device, the system's libraries, and the
# gateway-ip.txt that an owner may write for every app at once (source/native/src/diagnostic_gateway.h). The plugins
# only read that one, and no package places or removes it.
KNOWN_PATHS = ("/dev/null", "/var/opt/hmi/data/ID5/gateway-ip.txt")
KNOWN_FOLDERS = ("/lib/", "/usr/lib/", "/opt/sys/lib/")

EM_ARM, ET_EXEC, ET_DYN = 40, 2, 3


class Refused(Exception):
    """Why a build, or a step of it, is refused."""


# ---- the app table --------------------------------------------------------------------------------------------

def load_table(root=ROOT):
    """apps.json, checked. Returns the table with `apps` as {key: app}, in the file's order."""
    path = os.path.join(root, TABLE)
    try:
        with open(path, "rb") as f:
            table = json.loads(f.read().decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, ValueError) as e:
        raise Refused("%s cannot be read: %s" % (TABLE, e))

    def bad(message):
        raise Refused("%s: %s" % (TABLE, message))

    if not isinstance(table, dict):
        bad("is not a JSON object")
    known = {"namespace", "requires_agent", "plugins_root", "base_size", "generated_sizes", "apps"}
    if set(table) != known:
        bad("has the keys %s, not %s" % (sorted(table), sorted(known)))
    if not isinstance(table["namespace"], str) or not PACKAGE_ID.fullmatch(table["namespace"]):
        bad("namespace is not a package id prefix")
    if not isinstance(table["requires_agent"], str) or not table["requires_agent"]:
        bad("requires_agent is not a version range")
    root_dir = table["plugins_root"]
    if not isinstance(root_dir, str) or not re.fullmatch(r"(/[A-Za-z0-9._-]+)+", root_dir):
        bad("plugins_root is not an absolute folder")
    sizes = table["generated_sizes"]
    if not isinstance(table["base_size"], str) or not SIZE.fullmatch(table["base_size"]):
        bad("base_size is not WxH")
    if (not isinstance(sizes, list) or not all(isinstance(s, str) and SIZE.fullmatch(s) for s in sizes)
            or len(set(sizes)) != len(sizes) or table["base_size"] in sizes):
        bad("generated_sizes is not a list of distinct WxH sizes other than base_size")
    if not isinstance(table["apps"], list) or not table["apps"]:
        bad("apps is not a list of apps")

    apps = {}
    folders, connectors = set(), set()
    required = {"key", "name", "folder", "zip", "workers", "connector", "images", "layouts"}
    optional = {"conflicts", "licenses", "base_layout_at", "written"}
    for app in table["apps"]:
        if not isinstance(app, dict):
            bad("an app is not an object")
        where = "app %r" % app.get("key")
        if not required <= set(app) or not set(app) <= required | optional:
            bad("%s has the keys %s; it needs %s and may have %s"
                % (where, sorted(app), sorted(required), sorted(optional)))
        key = app["key"]
        if not isinstance(key, str) or not APP_KEY.fullmatch(key) or key in apps:
            bad("%s: the key is not a label of a-z, 0-9 and single hyphens, or is there twice" % where)
        pkg_id = "%s.%s" % (table["namespace"], key)
        if len(pkg_id) > 128:
            bad("%s: the id %s is longer than 128 characters" % (where, pkg_id))
        if not isinstance(app["name"], str) or not 1 <= len(app["name"]) <= 64 \
                or re.search(r"[\x00-\x1f\x7f]", app["name"]):
            bad("%s: the name is not 1 to 64 printable characters" % where)
        if not isinstance(app["folder"], str) or not FOLDER.fullmatch(app["folder"]) or app["folder"] in folders:
            bad("%s: the folder is not a name of A-Z, a-z, 0-9 and _, or is there twice" % where)
        folders.add(app["folder"])
        if not isinstance(app["zip"], str) or not ZIP_PATH.fullmatch(app["zip"]):
            bad("%s: zip is not packages/<name>.zip" % where)
        workers = app["workers"]
        if (not isinstance(workers, list) or not workers or len(set(workers)) != len(workers)
                or not all(isinstance(w, str) and WORKER.fullmatch(w) for w in workers)):
            bad("%s: workers is not a list of distinct program names ending in -qnx" % where)
        connector = app["connector"]
        if (not isinstance(connector, int) or isinstance(connector, bool) or not 1 <= connector <= 999999999
                or connector in connectors):
            bad("%s: connector is not a whole number, or is there twice" % where)
        connectors.add(connector)
        for flag in ("images", "layouts"):
            if not isinstance(app[flag], bool):
                bad("%s: %s is not true or false" % (where, flag))
        if app["layouts"] and not app["images"]:
            bad("%s: a layout is an XML file and an image zip, so layouts needs images" % where)
        licenses = app.get("licenses", [])
        if (not isinstance(licenses, list) or len(set(licenses)) != len(licenses)
                or not all(isinstance(p, str) and REL_PATH.fullmatch(p) and p != PROJECT_LICENSE for p in licenses)):
            bad("%s: licenses is not a list of distinct relative paths (other than %s)" % (where, PROJECT_LICENSE))
        names = [os.path.basename(p) for p in [PROJECT_LICENSE] + licenses]
        if len(set(names)) != len(names):
            bad("%s: two licence files have the same name" % where)
        base_at = app.get("base_layout_at", [])
        if (not isinstance(base_at, list) or len(set(map(str, base_at))) != len(base_at)
                or not all(isinstance(size, str) and size in sizes for size in base_at)):
            bad("%s: base_layout_at is not a list of distinct sizes from generated_sizes" % where)
        if base_at and not app["layouts"]:
            bad("%s: base_layout_at is for an app with layouts" % where)
        written = app.get("written", [])
        if (not isinstance(written, list) or len(set(map(str, written))) != len(written)
                or not all(isinstance(p, str) and len(p) <= 200 and ABS_FILE.fullmatch(p)
                           and not re.search(r"/\.\.?(?:/|$)", p) and not _is_under(p, root_dir)
                           and not _is_known(p) for p in written)):
            bad("%s: written is not a list of distinct absolute file paths outside %s and the system's folders"
                % (where, root_dir))
        if written and not app["layouts"]:
            bad("%s: written needs the preRemove hook, which only an app with layouts has" % where)
        apps[key] = dict(app, id=pkg_id)

    for key, app in apps.items():
        conflicts = app.get("conflicts", [])
        if (not isinstance(conflicts, list) or len(set(conflicts)) != len(conflicts)
                or not all(isinstance(c, str) and c in apps and c != key for c in conflicts)):
            bad("app %r: conflicts is not a list of other apps' keys" % key)
    return dict(table, apps=apps)


def _no_duplicates(items):
    obj = {}
    for key, value in items:
        if key in obj:
            raise ValueError("the key %r is there twice" % key)
        obj[key] = value
    return obj


def _is_under(path, folder):
    return path == folder or path.startswith(folder + "/")


def _is_known(path):
    return path in KNOWN_PATHS or any(path.startswith(folder) for folder in KNOWN_FOLDERS)


def all_sizes(table):
    return [table["base_size"]] + list(table["generated_sizes"])


def layout_sizes(table, app):
    """The sizes the app's package holds a layout for: every size but those in its base_layout_at, where it
    shows the base layout."""
    return [size for size in all_sizes(table) if size not in app.get("base_layout_at", [])]


def plugin_files(app):
    """The exact names in the app's plugin folder."""
    folder = app["folder"]
    names = ["lib%s.so" % folder, "%s.manifest" % folder, "%s.xml" % folder] + list(app["workers"])
    if app["images"]:
        names.append(image_zip(folder))
    return names


def image_zip(folder):
    return "%s_ALL_Images.zip" % folder


# ---- reading and checking the upstream plugin -----------------------------------------------------------------

def read_plugin(root, app):
    """{name: bytes} of the app's plugin folder in its upstream zip, with the zip's SHA256SUMS checked.
    Returns (files, note), where note says how the files were pinned."""
    path = os.path.join(root, app["zip"])
    folder = app["folder"]
    prefix = folder + "/"
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise Refused("%s cannot be read as a zip: %s" % (app["zip"], e))
    with archive:
        names = [info.filename for info in archive.infolist()]
        if len(set(names)) != len(names):
            raise Refused("%s holds a name twice" % app["zip"])
        found = {}
        for info in archive.infolist():
            if not info.filename.startswith(prefix) or info.is_dir():
                continue
            if info.file_size > MAX_MEMBER:
                raise Refused("%s: %s is larger than %d bytes" % (app["zip"], info.filename, MAX_MEMBER))
            try:
                found[info.filename[len(prefix):]] = archive.read(info)
            except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as e:
                raise Refused("%s: %s cannot be read: %s" % (app["zip"], info.filename, e))
        sums = archive.read(SUMS) if SUMS in names else None

    expected = set(plugin_files(app))
    missing, extra = sorted(expected - set(found)), sorted(set(found) - expected)
    if missing or extra:
        parts = []
        if missing:
            parts.append("lacks %s" % ", ".join(missing))
        if extra:
            parts.append("holds %s, which this script does not know" % ", ".join(extra))
        raise Refused("%s: the folder %s %s. A plugin folder holds exactly: %s. If upstream changed the app, "
                      "update nbtapps/apps.json and this script to match, after checking what the change is"
                      % (app["zip"], prefix, "; ".join(parts), ", ".join(sorted(expected))))

    if sums is None:
        note = "%s has no %s: the files are pinned by this commit only" % (app["zip"], SUMS)
    else:
        check_sums(app["zip"], sums, prefix, found)
        note = "%s: %s matches all %d files of %s" % (app["zip"], SUMS, len(found), prefix)
    return found, note


def parse_sums(data, where):
    """sha256sum output -> {name: hex}. Raises Refused on a line of another form or a name given twice."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("%s is not UTF-8" % where)
    sums = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](\S.*)", line)
        if not match:
            raise Refused("%s, line %d, is not a sha256sum line" % (where, number))
        if match.group(2) in sums:
            raise Refused("%s names %s twice" % (where, match.group(2)))
        sums[match.group(2)] = match.group(1)
    return sums


def check_sums(zip_name, data, prefix, found):
    sums = parse_sums(data, "%s's %s" % (zip_name, SUMS))
    for name, content in sorted(found.items()):
        listed = sums.get(prefix + name)
        if listed is None:
            raise Refused("%s's %s does not list %s%s" % (zip_name, SUMS, prefix, name))
        if hashlib.sha256(content).hexdigest() != listed:
            raise Refused("%s: %s%s is not the file its %s pins" % (zip_name, prefix, name, SUMS))
    for name in sorted(sums):
        if name.startswith(prefix) and name[len(prefix):] not in found:
            raise Refused("%s's %s lists %s, which is not in the zip" % (zip_name, SUMS, name))


def check_elf(name, data, kinds):
    """A 32-bit little-endian ARM ELF file of one of `kinds` (e_type values)."""
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise Refused("%s is not an ELF file" % name)
    if data[4] != 1 or data[5] != 1:
        raise Refused("%s is not a 32-bit little-endian ELF file" % name)
    e_type, e_machine = struct.unpack_from("<HH", data, 16)
    if e_machine != EM_ARM:
        raise Refused("%s is not built for ARM (e_machine %d)" % (name, e_machine))
    if e_type not in kinds:
        raise Refused("%s is not a %s (e_type %d)"
                      % (name, "shared library" if kinds == (ET_DYN,) else "program", e_type))


def parse_xml(name, data):
    """An XML file with no DOCTYPE or ENTITY. Returns its root element."""
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", data, re.I):
        raise Refused("%s has a document type or entity declaration" % name)
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise Refused("%s is not well-formed XML: %s" % (name, e))


def check_description(name, data):
    """A plugin's UI description: XML, and no entryButtonFocusID (the catalogue has none to give these apps)."""
    tree = parse_xml(name, data)
    if any("entryButtonFocusID" in element.attrib for element in tree.iter()):
        raise Refused("%s has an entryButtonFocusID, which this packaging does not expect: the focus id would "
                      "have to be given to the package by the catalogue first" % name)


def check_image_zip(name, data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as images:
            damaged = images.testzip()
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError) as e:
        raise Refused("%s is not a readable zip: %s" % (name, e))
    if damaged is not None:
        raise Refused("%s is not a readable zip: %s in it is damaged" % (name, damaged))


def _texts(tree, tag):
    return [(element.text or "").strip() for element in tree.iter(tag)]


def check_plugin(table, app, files):
    """Every check of the plugin folder's own files."""
    folder = app["folder"]
    where = "%s/" % folder
    lib = "lib%s.so" % folder
    check_elf(where + lib, files[lib], (ET_DYN,))
    for worker in app["workers"]:
        check_elf(where + worker, files[worker], (ET_EXEC, ET_DYN))
        wanted = ("%s/%s/%s" % (table["plugins_root"], folder, worker)).encode("ascii")
        if wanted + b"\0" not in files[lib]:
            raise Refused("%s does not hold the path %s, where it would start its worker: the plugin would not "
                          "work from %s/%s/" % (where + lib, wanted.decode(), table["plugins_root"], folder))

    check_paths(table, app, files)

    name = where + "%s.manifest" % folder
    tree = parse_xml(name, files["%s.manifest" % folder])
    if tree.tag != "plugin":
        raise Refused("%s is not a <plugin>" % name)

    def one(tag, value):
        found = _texts(tree, tag)
        if found != [value]:
            raise Refused("%s has %s %s, not exactly %r" % (name, tag, found or "none", value))

    one("name", folder)
    one("file", "{pluginDir}/%s" % folder)
    one("DescFile", "{pluginDir}/%s.xml" % folder)
    if app["images"]:
        one("ImageDBFile", "{pluginDir}/%s" % image_zip(folder))
    elif _texts(tree, "ImageDBFile"):
        raise Refused("%s names an ImageDBFile, and apps.json says the app has no images" % name)
    ids = [(element.text or "").strip() for parent in tree.iter("pluginAppConnector") for element in parent.iter("id")]
    if ids != [str(app["connector"])]:
        raise Refused("%s claims the connector id(s) %s, not exactly %d as apps.json says"
                      % (name, ", ".join(ids) or "none", app["connector"]))

    check_description(where + "%s.xml" % folder, files["%s.xml" % folder])
    if app["images"]:
        check_image_zip(where + image_zip(folder), files[image_zip(folder)])


def path_strings(data):
    """The absolute paths a binary holds as whole strings (PATH_STRING), sorted."""
    return sorted({match.group(0).decode("ascii") for match in PATH_STRING.finditer(data)})


def check_paths(table, app, files):
    """Every absolute path in the library and the workers is one this packaging knows: the system's, one in the
    plugin's own folder, or one apps.json names under `written`, and each of those is in the library. A removal
    takes away the package's files and what the preRemove hook removes, so a path the plugin writes to that is
    named nowhere would stay on the head unit."""
    folder = app["folder"]
    own = "%s/%s" % (table["plugins_root"], folder)
    written = list(app.get("written", []))
    lib = "lib%s.so" % folder
    for name in [lib] + list(app["workers"]):
        for path in path_strings(files[name]):
            if _is_known(path) or _is_under(path, own) or path in written:
                continue
            raise Refused("%s/%s holds the path %s, which this packaging does not know. If the plugin writes "
                          "there, removing the package would leave the file: after checking what it is, name it "
                          "under \"written\" for the app %s in %s, and the preRemove hook removes it"
                          % (folder, name, path, app["key"], TABLE))
    for path in written:
        if path.encode("ascii") + b"\0" not in files[lib]:
            raise Refused("%s names %s as written by the app %s, and %s/%s does not hold that path"
                          % (TABLE, path, app["key"], folder, lib))


# ---- layouts and hooks ----------------------------------------------------------------------------------------

def layout_needs(table, app):
    """The files the layout and hook steps provide for a layout app: ([layout paths], [hook paths]), relative to
    the repo."""
    folder = app["folder"]
    base = "%s/%s" % (LAYOUTS, folder)
    layouts = ["%s/SOURCE" % base]
    for size in table["generated_sizes"]:
        layouts += ["%s/%s/%s.xml" % (base, size, folder), "%s/%s/%s" % (base, size, image_zip(folder))]
    hooks = ["%s/%s.in" % (HOOKS, script) for _, script in HOOK_TEMPLATES]
    return layouts, hooks


def read_layouts(root, table, app, files):
    """{size: {name: bytes}} of every size, the base size from the upstream zip. Refuses missing, stray or stale
    layout files, and missing hook templates."""
    folder = app["folder"]
    layouts, hooks = layout_needs(table, app)
    missing_layouts = [p for p in layouts if not os.path.isfile(os.path.join(root, p))]
    missing_hooks = [p for p in hooks if not os.path.isfile(os.path.join(root, p))]
    if missing_layouts or missing_hooks:
        lines = ["%s (%s) shows a layout per screen size and cannot be built yet:" % (app["key"], folder)]
        if missing_layouts:
            lines.append("  missing, from the layout step (generating the %s layouts from the %s one):"
                         % (", ".join(table["generated_sizes"]), table["base_size"]))
            lines += ["    " + p for p in missing_layouts]
        if missing_hooks:
            lines.append("  missing, from the hook step (the postInstall and preRemove scripts that pick a layout):")
            lines += ["    " + p for p in missing_hooks]
        lines.append("  nbtapps/README.md, \"Layouts and hooks\", has the contract both steps follow.")
        raise Refused("\n".join(lines))

    base = os.path.join(root, LAYOUTS, folder)
    allowed = {"SOURCE"} | set(table["generated_sizes"])
    stray = sorted(set(os.listdir(base)) - allowed)
    if stray:
        raise Refused("%s/%s holds %s, which is no size in apps.json" % (LAYOUTS, folder, ", ".join(stray)))

    xml_name, zip_name = "%s.xml" % folder, image_zip(folder)
    with open(os.path.join(base, "SOURCE"), "rb") as f:
        source = parse_sums(f.read(), "%s/%s/SOURCE" % (LAYOUTS, folder))
    pinned = {"%s/%s" % (folder, xml_name): files[xml_name], "%s/%s" % (folder, zip_name): files[zip_name]}
    if set(source) != set(pinned):
        raise Refused("%s/%s/SOURCE names %s; it names exactly %s"
                      % (LAYOUTS, folder, ", ".join(sorted(source)) or "nothing", " and ".join(sorted(pinned))))
    stale = sorted(name for name, data in pinned.items() if hashlib.sha256(data).hexdigest() != source[name])
    if stale:
        raise Refused("the layouts in %s/%s are stale: they were generated from another %s than the one in %s. "
                      "Generate them again from the files in the zip" % (LAYOUTS, folder, " and ".join(stale),
                                                                         app["zip"]))

    out = {table["base_size"]: {xml_name: files[xml_name], zip_name: files[zip_name]}}
    for size in table["generated_sizes"]:
        here = os.path.join(base, size)
        stray = sorted(set(os.listdir(here)) - {xml_name, zip_name})
        if stray:
            raise Refused("%s/%s/%s holds %s; a size holds %s and %s only"
                          % (LAYOUTS, folder, size, ", ".join(stray), xml_name, zip_name))
        out[size] = {}
        for name in (xml_name, zip_name):
            if os.path.islink(os.path.join(here, name)):
                raise Refused("%s/%s/%s/%s is a link" % (LAYOUTS, folder, size, name))
            with open(os.path.join(here, name), "rb") as f:
                out[size][name] = f.read()
        where = "%s/%s/%s/" % (LAYOUTS, folder, size)
        check_description(where + xml_name, out[size][xml_name])
        check_image_zip(where + zip_name, out[size][zip_name])
    return out


def stamp_hooks(root, table, app):
    """{script name: bytes} of the hook templates with their placeholders filled in: @FOLDER@, @BASE_SIZE@,
    @SIZES@ (the sizes the package holds a layout for, as a case pattern), @BASE_AT@ (the sizes of base_layout_at
    as a case pattern, or one that matches no size) and @WRITTEN@ (the paths of `written`, with spaces between)."""
    values = {"@FOLDER@": app["folder"], "@BASE_SIZE@": table["base_size"],
              "@SIZES@": "|".join(layout_sizes(table, app)),
              "@BASE_AT@": "|".join(app.get("base_layout_at", [])) or "none",
              "@WRITTEN@": " ".join(app.get("written", []))}
    out = {}
    for _, script in HOOK_TEMPLATES:
        path = "%s/%s.in" % (HOOKS, script)
        try:
            with open(os.path.join(root, path), "rb") as f:
                text = f.read().decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("%s is not UTF-8" % path)
        for name, value in values.items():
            text = text.replace(name, value)
        left = sorted(set(PLACEHOLDER.findall(text)))
        if left:
            raise Refused("%s has the placeholder(s) %s, which this script does not fill in (it fills in %s)"
                          % (path, ", ".join(left), ", ".join(sorted(values))))
        out[script] = text.encode("utf-8")
    return out


# ---- the package source folder --------------------------------------------------------------------------------

def make_manifest(table, app, version):
    """manifest.json without `contents`."""
    folder = app["folder"]
    files = [{"src": "payload/%s/" % folder, "dest": "${var_plugins}/%s/" % folder, "mode": "0644"}]
    files += [{"src": "payload/bin/%s" % worker, "dest": "${var_plugins}/%s/%s" % (folder, worker), "mode": "0755"}
              for worker in app["workers"]]
    install = {"files": files}
    if app["layouts"]:
        install["hooks"] = {kind: {"script": "hooks/%s" % script, "timeout": HOOK_TIMEOUT}
                            for kind, script in HOOK_TEMPLATES}
    install["reboot"] = "hmi-restart"
    manifest = {"manifestVersion": 1, "id": app["id"], "name": app["name"], "version": version,
                "requires": {"agent": table["requires_agent"]}}
    if app.get("conflicts"):
        manifest["conflicts"] = [{"id": table["apps"][key]["id"]} for key in app["conflicts"]]
    manifest["install"] = install
    return manifest


def stage(root, table, app, version, files, layouts=None, hooks=None):
    """{archive path: bytes} of the package source folder, and the manifest."""
    folder = app["folder"]
    out = {}
    plugin = "payload/%s/" % folder
    out[plugin + "lib%s.so" % folder] = files["lib%s.so" % folder]
    out[plugin + "%s.manifest" % folder] = files["%s.manifest" % folder]
    for worker in app["workers"]:
        out["payload/bin/" + worker] = files[worker]
    if app["layouts"]:
        for size in layout_sizes(table, app):
            held = layouts[size]
            for name, data in held.items():
                out["%slayouts/%s/%s" % (plugin, size, name)] = data
        for script, data in hooks.items():
            out["hooks/" + script] = data
    else:
        out[plugin + "%s.xml" % folder] = files["%s.xml" % folder]
        if app["images"]:
            out[plugin + image_zip(folder)] = files[image_zip(folder)]
    for path in [PROJECT_LICENSE] + list(app.get("licenses", [])):
        full = os.path.join(root, path)
        if not os.path.isfile(full) or os.path.islink(full):
            raise Refused("the licence file %s is not there" % path)
        with open(full, "rb") as f:
            out["licenses/" + os.path.basename(path)] = f.read()
    return out, make_manifest(table, app, version)


def write_source(directory, manifest, staged):
    with open(os.path.join(directory, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    for path, data in staged.items():
        full = os.path.join(directory, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)


# ---- nbtpkg.py, keys and the working tree ---------------------------------------------------------------------

def load_nbtpkg(headunit):
    """NBTApps' tools/nbtpkg.py, loaded from the headunit/ folder of a checkout."""
    if not headunit:
        raise Refused("nbtpkg.py is needed: give --nbtapps DIR or set %s to the headunit/ folder of an NBTApps "
                      "checkout" % HEADUNIT_ENV)
    path = os.path.join(headunit, "tools", "nbtpkg.py")
    if not os.path.isfile(path) or not os.path.isfile(os.path.join(headunit, "spec", "manifest.schema.json")):
        raise Refused("%s is not the headunit/ folder of an NBTApps checkout: it lacks tools/nbtpkg.py or "
                      "spec/manifest.schema.json" % headunit)
    spec = importlib.util.spec_from_file_location("nbtapps_nbtpkg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_key_file(nbtpkg, path, what):
    """A key file through nbtpkg.read_key, which exits on a bad file; here that is a refusal."""
    if not os.path.isfile(path):
        raise Refused("%s %s is not there" % (what, path))
    try:
        return nbtpkg.read_key(path)
    except SystemExit:
        raise Refused("%s %s is not a key file (one line of 64 lowercase hex characters)" % (what, path))


def resolve_key(nbtpkg, key_file, environ, require):
    """The private key's 32 bytes, or None for an unsigned build. Returns (seed, where it came from)."""
    from_env = environ.get(KEY_ENV)
    if key_file and from_env:
        raise Refused("a key is given both with --key and in %s; give one" % KEY_ENV)
    if key_file:
        return _read_key_file(nbtpkg, key_file, "the key file"), "--key"
    if from_env:
        text = from_env.strip()
        if not KEY_HEX.fullmatch(text):
            raise Refused("%s is not 64 hex characters" % KEY_ENV)   # never says what it holds
        return bytes.fromhex(text), KEY_ENV
    if require:
        raise Refused("--require-key: no key was given (--key FILE or %s)" % KEY_ENV)
    return None, None


def check_pair(nbtpkg, seed, pub):
    try:
        public = nbtpkg.public_key(seed)
    except ValueError:
        raise Refused("the private key is not an Ed25519 key")
    if public != pub:
        raise Refused("the private key is not the private half of the public key given (fingerprint %s); "
                      "its own public half has fingerprint %s" % (nbtpkg.fingerprint(pub), nbtpkg.fingerprint(public)))


def git(root, *args):
    try:
        result = subprocess.run(("git", "-C", root) + args, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused("git %s: %s" % (args[0], e))
    if result.returncode != 0:
        raise Refused("git %s: %s" % (args[0], result.stderr.decode("utf-8", "replace").strip()))
    return result.stdout


def tree_state(root):
    """(HEAD's 40-hex commit, [paths under packages/, source/ or nbtapps/ with changes not committed])."""
    commit = git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii", "replace").strip()
    if not COMMIT.fullmatch(commit):
        raise Refused("git rev-parse HEAD gave %r, not a 40-hex commit" % commit[:80])
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *WATCHED)
    changed = sorted({entry[3:].decode("utf-8", "replace") for entry in status.split(b"\0") if len(entry) > 3})
    return commit, changed


# ---- the record -----------------------------------------------------------------------------------------------

def tag_of(app, version):
    return "%s-v%s" % (app["key"], version)


def make_record(app, version, archive_sha256, repo, commit, released, notes):
    """nbtpkg-record.json's value, as tools/propose.py in nbtapps-packages reads it."""
    name = "%s-%s.nbtpkg" % (app["id"], version)
    record = {"url": "https://github.com/%s/releases/download/%s/%s" % (repo, tag_of(app, version), name),
              "sha256": archive_sha256, "released": released}
    if notes:
        record["notes"] = list(notes)
    record["source"] = {"repo": "https://github.com/%s" % repo, "commit": commit}
    return {"id": app["id"], "version": version, "record": record}


# ---- arguments ------------------------------------------------------------------------------------------------

def check_version(text):
    if len(text) > 64 or not VERSION.fullmatch(text) or text.endswith(".lock"):
        raise Refused("--version %r is not X.Y.Z with an optional pre-release part such as -beta.1 (no build "
                      "metadata, no leading zeros)" % text[:80])
    return text


def check_repo(text):
    owner_name = text.split("/", 1)
    if not REPO.fullmatch(text) or owner_name[1] in (".", "..") or owner_name[1].endswith(".git"):
        raise Refused("--repo %r is not OWNER/REPO" % text[:80])
    return text


def check_released(text):
    if not DATE.fullmatch(text):
        raise Refused("--released %r is not YYYY-MM-DD" % text[:80])
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        raise Refused("--released %s is not a date" % text)
    return text


def check_notes(notes):
    for note in notes:
        if not 1 <= len(note) <= 200 or not NOTE.fullmatch(note):
            raise Refused("a --note is 1 to 200 characters with no control character and not only spaces: %r"
                          % note[:80])
    return list(notes)


# ---- build ----------------------------------------------------------------------------------------------------

def build(root, table, key, version, out, nbtpkg, seed=None, pub=None, released=None, notes=(),
          repo=DEFAULT_REPO, allow_dirty=False, log=print):
    """Builds one app's package into `out`. `seed` signs it, and then `pub` must be its public half. Returns a dict:
    archive (path), sha256, size, flags, manifest, record (path or None)."""
    if key not in table["apps"]:
        raise Refused("--app %r is none of: %s" % (key[:80], ", ".join(table["apps"])))
    app = table["apps"][key]
    check_version(version)
    check_repo(repo)
    released = check_released(released) if released else datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    notes = check_notes(notes)

    commit = None
    if seed is not None:
        if pub is None:
            raise Refused("a signed build needs the public key to check the private one against")
        check_pair(nbtpkg, seed, pub)
        commit, changed = tree_state(root)
        if changed:
            shown = ", ".join(changed[:5]) + (" and %d more" % (len(changed) - 5) if len(changed) > 5 else "")
            if not allow_dirty:
                raise Refused("the working tree has changes under %s that are not committed (%s). A signed build "
                              "names HEAD as its source, so commit first, or give --allow-dirty to build without a "
                              "record" % (", ".join(w + "/" for w in WATCHED), shown))
            log("--allow-dirty: the tree has changes that are not committed (%s); the archive is signed, and no "
                "%s is written" % (shown, RECORD))
            commit = None

    files, note = read_plugin(root, app)
    log(note)
    check_plugin(table, app, files)
    log("%s/: checked (%s)" % (app["folder"], ", ".join(sorted(files))))
    layouts = hooks = None
    if app["layouts"]:
        layouts = read_layouts(root, table, app, files)
        hooks = stamp_hooks(root, table, app)
        log("layouts: %s; hooks: %s" % (", ".join(layout_sizes(table, app)), ", ".join(sorted(hooks))))
        if app.get("base_layout_at"):
            log("at %s the app shows the %s layout" % (", ".join(app["base_layout_at"]), table["base_size"]))
        if app.get("written"):
            log("preRemove also removes: %s" % ", ".join(app["written"]))
    staged, manifest = stage(root, table, app, version, files, layouts, hooks)

    with tempfile.TemporaryDirectory(prefix="nbtpkg-stage-") as work:
        write_source(work, manifest, staged)
        try:
            built, data = nbtpkg.build_package(work, seed)
            info = nbtpkg.verify_package(data, keys=[pub] if seed is not None else None,
                                         allow_unsigned=seed is None)
        except nbtpkg.PkgError as e:
            raise Refused("nbtpkg.py refused the package: %s: %s" % (e.code, e))
    if (info["id"], info["version"]) != (app["id"], version):
        raise Refused("the archive says it is %s %s" % (info["id"], info["version"]))

    os.makedirs(out, exist_ok=True)
    archive = os.path.join(out, "%s-%s.nbtpkg" % (app["id"], version))
    _write(archive, data)
    log("%s  %d bytes, sha256 %s" % (archive, info["size"], info["sha256"]))
    if seed is not None:
        log("signed, key %s; verified with that public key" % nbtpkg.fingerprint(pub))
    else:
        log("NOT signed: no key was given")
    log("flags: %s" % (", ".join(info["flags"]) or "none"))

    record_path = None
    if commit is not None:
        record = make_record(app, version, info["sha256"], repo, commit, released, notes)
        record_path = os.path.join(out, RECORD)
        _write(record_path, (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        log("%s  for release %s of %s, commit %s" % (record_path, tag_of(app, version), repo, commit))
    elif seed is None:
        log("no %s: only a signed build has one" % RECORD)
    return {"archive": archive, "sha256": info["sha256"], "size": info["size"], "flags": info["flags"],
            "manifest": built, "record": record_path}


def _write(path, data):
    part = path + ".part"
    with open(part, "wb") as f:
        f.write(data)
    os.replace(part, path)


# ---- command line ---------------------------------------------------------------------------------------------

def cmd_list(args, table, root):
    print("%-20s %-38s %-19s %-14s %-24s %-9s %s" % ("key", "id", "name", "folder", "zip", "connector", "notes"))
    for key, app in table["apps"].items():
        notes = ["workers " + ", ".join(app["workers"])]
        notes.append("layouts %s" % "|".join(layout_sizes(table, app)) if app["layouts"] else
                     "%s layout only" % table["base_size"] if app["images"] else "no images")
        if app.get("conflicts"):
            notes.append("conflicts with " + ", ".join(app["conflicts"]))
        if app.get("base_layout_at"):
            notes.append("the %s layout at %s" % (table["base_size"], ", ".join(app["base_layout_at"])))
        if app.get("written"):
            notes.append("writes " + ", ".join(app["written"]))
        if app.get("licenses"):
            notes.append("also " + ", ".join(app["licenses"]))
        print("%-20s %-38s %-19s %-14s %-24s %-9d %s" % (key, app["id"], app["name"], app["folder"], app["zip"],
                                                         app["connector"], "; ".join(notes)))
    print("Every package installs into ${var_plugins}/<folder>/ (%s on a car), needs agent %s, and asks for an "
          "HMI restart." % (table["plugins_root"], table["requires_agent"]))


def cmd_build(args, table, root):
    # The cheap argument checks come before anything that reads a key or a zip.
    if args.app not in table["apps"]:
        raise Refused("--app %r is none of: %s" % (args.app[:80], ", ".join(table["apps"])))
    nbtpkg = load_nbtpkg(args.nbtapps or os.environ.get(HEADUNIT_ENV))
    check_version(args.version)
    check_repo(args.repo)
    if args.released:
        check_released(args.released)
    check_notes(args.note)
    seed, source = resolve_key(nbtpkg, args.key, os.environ, args.require_key)
    pub = None
    if seed is not None:
        pub_path = args.pub or os.path.join(root, AUTHOR_PUB)
        if not args.pub and not os.path.isfile(pub_path):
            raise Refused("a signed build needs %s, the author key's public half, and there is none yet (or give "
                          "--pub FILE)" % AUTHOR_PUB)
        pub = _read_key_file(nbtpkg, pub_path, "the public key")
        print("signing with the key from %s" % source)
    build(root, table, args.app, args.version, args.out, nbtpkg, seed=seed, pub=pub, released=args.released,
          notes=args.note, repo=args.repo, allow_dirty=args.allow_dirty)


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(prog="make_nbtpkg.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print the app table").set_defaults(run=cmd_list)
    p = sub.add_parser("build", help="build one app's package")
    p.add_argument("--app", required=True, help="the app's key, as `list` prints it")
    p.add_argument("--version", required=True, help="X.Y.Z, with an optional pre-release part")
    p.add_argument("--out", default=".", help="folder for the archive and the record (default: .)")
    p.add_argument("--key", help="private key file; %s instead holds the key as 64 hex characters" % KEY_ENV)
    p.add_argument("--require-key", action="store_true", help="fail if no key is given")
    p.add_argument("--pub", help="public key file to check the key against (default: %s)" % AUTHOR_PUB)
    p.add_argument("--released", help="the record's date, YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--note", action="append", default=[], help="a line of the record's notes; may be repeated")
    p.add_argument("--repo", default=DEFAULT_REPO, help="the GitHub repo the release is in (default: %(default)s)")
    p.add_argument("--allow-dirty", action="store_true",
                   help="sign a tree with changes that are not committed, and write no record")
    p.add_argument("--nbtapps", help="the headunit/ folder of an NBTApps checkout (default: $%s)" % HEADUNIT_ENV)
    p.set_defaults(run=cmd_build)
    args = parser.parse_args(argv)
    try:
        table = load_table(root)
        args.run(args, table, root)
    except Refused as e:
        sys.exit("make_nbtpkg: %s" % e)
    except OSError as e:
        sys.exit("make_nbtpkg: %s" % e)


if __name__ == "__main__":
    main()
