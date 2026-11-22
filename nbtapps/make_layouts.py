#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generates the layouts of the apps that show one per screen size, from upstream's 1280x480 layout.

  make_layouts.py [APP ...]            generate the layouts of every app with "layouts": true, or of those named
  make_layouts.py --check [APP ...]    generate them in memory and compare them with what is committed

An app is named by its key in apps.json. For each size in apps.json's generated_sizes, with one factor
f = content_width(size) / content_width(base_size) along both axes (make_nbtpkg.content_width: the layout units
of the part of the screen an app gets, 720 at 800x480, 900 at 960x480 and 1260 on every 8:3 screen):

- in <Folder>.xml, the value of each <property id="20"> (x), <property id="9"> (width), <property id="21"> (y) and
  <property id="10"> (height) is multiplied by f. The file is changed as text: every other byte stays as it is in
  the base file;
- each PNG in <Folder>_ALL_Images.zip is resized to (width x f, height x f), in RGBA with a Lanczos filter. An
  image whose size does not change is copied as it is;
- every result is rounded to the nearest whole number, halves to the even one, and a width or height is at least 1.

This is the rule of upstream's own desktop installer since v0.7.5 (source/tools/desktop-installer/
stage-payload.py), from Andreas Boehm's measurements in upstream's issue #113. The head unit scales a layout to an
8:3 screen itself, so 1440x540 and 1920x720 (base_shown_at) get the base layout and none of their own. The output goes to nbtapps/layouts/<Folder>/, with a SOURCE
file that pins the two base files by their SHA-256. README.md beside this file has the contract.

The base files come from the upstream zip through make_nbtpkg.py, with all its checks. This script refuses a base
file it does not know how to scale: a property id other than those it knows, a value to scale that is not a plain
non-negative whole number, a <property> written in another form, or an image zip entry that is not a PNG named
<digits>.png.

--check compares the XML and SOURCE byte for byte, and the images by their size and decoded pixels, since PNG and
zip bytes can differ between zlib builds. It writes nothing, and exits with 1 and names each difference.

Needs python3 and Pillow. The build (make_nbtpkg.py) and the release workflow do not run this script.
"""
import argparse
import hashlib
import io
import os
import re
import shutil
import struct
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ElementTree
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_nbtpkg as m  # noqa: E402

Refused = m.Refused

X, Y, WIDTH, HEIGHT = "20", "21", "9", "10"
SCALED = {X: "x", Y: "y", WIDTH: "w", HEIGHT: "h"}       # the property ids this script scales, and along what
KEPT = {"2", "3", "23"}                                   # the ids it knows and leaves alone
WHOLE = re.compile(rb"0|[1-9][0-9]*")
PROPERTY = re.compile(rb"<property\b([^>]*)>")
ATTRIBUTE = re.compile(rb'\s+([A-Za-z_][A-Za-z0-9_.-]*)="([^"<&]*)"')
ENTRY = re.compile(r"[0-9]+\.png")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)                          # as most of upstream's entries have it
ZIP_MODE = 0o600 << 16                                    # as upstream's entries have it
UNIX = 3


def pillow():
    """Pillow's Image module, or a refusal that says how to get it."""
    try:
        from PIL import Image
    except ImportError:
        raise Refused("this script needs Pillow to resize the images: python3 -m pip install Pillow")
    return Image


# ---- the rule -------------------------------------------------------------------------------------------------

def parse_size(text):
    width, height = text.split("x")
    return int(width), int(height)


def factors(table, size):
    """(fx, fy) of a size, exact: both the share of the base layout's units that a screen of that size gives an
    app."""
    f = Fraction(m.content_width(size), m.content_width(table["base_size"]))
    return f, f


def scaled(value, factor, minimum=0):
    """value x factor, rounded to the nearest whole number with halves to the even one, and at least `minimum`."""
    return max(minimum, round(Fraction(value) * factor))


def scaled_size(width, height, fx, fy):
    return scaled(width, fx, 1), scaled(height, fy, 1)


# ---- the XML --------------------------------------------------------------------------------------------------

def geometry(name, data):
    """The values to scale in a base <Folder>.xml: [(start, end, id, value)], where data[start:end] is the value.
    Refuses a property this script does not know, or one written in a form it does not read."""
    tree = m.parse_xml(name, data)
    found, ids = [], []
    for match in PROPERTY.finditer(data):
        inside = match.group(1)
        body = inside[:-1] if inside.endswith(b"/") else inside
        at = match.start(1)
        attributes, spans, pos = {}, {}, 0
        for attribute in ATTRIBUTE.finditer(body):
            if attribute.start() != pos:
                break
            key = attribute.group(1).decode("ascii")      # never twice: the parser above refuses that
            attributes[key] = attribute.group(2)
            spans[key] = (at + attribute.start(2), at + attribute.end(2))
            pos = attribute.end()
        if body[pos:].strip() or set(attributes) != {"id", "value"}:
            raise Refused("%s: %s is not a <property id=\"..\" value=\"..\"> as this script reads them"
                          % (name, match.group(0).decode("utf-8", "replace")[:80]))
        pid = attributes["id"].decode("ascii", "replace")
        ids.append(pid)
        if pid in SCALED:
            value = attributes["value"]
            if not WHOLE.fullmatch(value):
                raise Refused("%s: <property id=\"%s\"> has the value %r, which is not a plain non-negative whole "
                              "number: this script does not know how to scale it"
                              % (name, pid, value.decode("utf-8", "replace")))
            found.append(spans["value"] + (pid, int(value)))
        elif pid not in KEPT:
            raise Refused("%s has <property id=\"%s\">, which this script does not know. It scales %s and leaves %s "
                          "alone; find out what the new id is before adding it to one of them"
                          % (name, pid, ", ".join(sorted(SCALED, key=int)), ", ".join(sorted(KEPT, key=int))))
    # Every <property> the text search found is one the parser sees, and none is missing (one in a comment, say).
    parsed = [element.get("id") for element in tree.iter("property")]
    if parsed != ids:
        raise Refused("%s: the <property> elements found in the text are not those the XML parser sees" % name)
    return found


def scale_xml(name, data, fx, fy):
    """The base XML with its geometry scaled, every other byte as it is."""
    factor = {"x": fx, "w": fx, "y": fy, "h": fy}
    out, pos = [], 0
    expected = []
    for start, end, pid, value in geometry(name, data):
        axis = SCALED[pid]
        new = scaled(value, factor[axis], 1 if axis in "wh" else 0)
        out += [data[pos:start], str(new).encode("ascii")]
        pos = end
        expected.append(str(new))
    out.append(data[pos:])
    result = b"".join(out)
    _same_but_geometry(name, data, result, expected)
    return result


def _same_but_geometry(name, base, result, values):
    """The parsed result is the parsed base, with only those values changed."""
    old, new = ElementTree.fromstring(base), ElementTree.fromstring(result)
    left, right = list(old.iter()), list(new.iter())
    values_seen = []
    if len(left) != len(right):
        raise Refused("%s: scaling changed the XML's structure" % name)
    for a, b in zip(left, right):
        if (a.tag, a.text, a.tail) != (b.tag, b.text, b.tail) or set(a.attrib) != set(b.attrib):
            raise Refused("%s: scaling changed the XML's structure" % name)
        for key in a.attrib:
            if a.get(key) != b.get(key):
                if a.tag != "property" or key != "value" or a.get("id") not in SCALED:
                    raise Refused("%s: scaling changed something other than geometry" % name)
            if a.tag == "property" and key == "value" and a.get("id") in SCALED:
                values_seen.append(b.get(key))
    if values_seen != values:
        raise Refused("%s: the scaled values are not where they were expected" % name)


# ---- the images -----------------------------------------------------------------------------------------------

def png_header(name, data):
    """(width, height, bit depth, colour type) from a PNG's IHDR. Refuses a file that is not a PNG."""
    if len(data) < 33 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise Refused("%s is not a PNG" % name)
    return struct.unpack(">IIBB", data[16:26])


def read_entries(name, data):
    """[(entry name, bytes)] of an image zip, in its order. Refuses an entry that is not <digits>.png."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as images:
            entries = []
            for info in images.infolist():
                if info.is_dir() or not ENTRY.fullmatch(info.filename):
                    raise Refused("%s holds %r; an image zip holds <digits>.png files only" % (name, info.filename))
                if info.file_size > m.MAX_MEMBER:
                    raise Refused("%s: %s is larger than %d bytes" % (name, info.filename, m.MAX_MEMBER))
                entries.append((info.filename, images.read(info)))
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError) as e:
        raise Refused("%s is not a readable zip: %s" % (name, e))
    names = [entry for entry, _ in entries]
    if len(set(names)) != len(names):
        raise Refused("%s holds a name twice" % name)
    return entries


def decode(name, data):
    """A PNG opened and loaded by Pillow. Refuses anything else."""
    Image = pillow()
    png_header(name, data)
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as e:      # Pillow raises many kinds on a bad file
        raise Refused("%s cannot be decoded as a PNG: %s" % (name, e))
    if image.format != "PNG":
        raise Refused("%s is not a PNG (%s)" % (name, image.format))
    return image


def scale_image(name, data, fx, fy):
    """The PNG resized by the rule; the same bytes when its size does not change."""
    Image = pillow()
    image = decode(name, data)
    size = scaled_size(image.width, image.height, fx, fy)
    if size == image.size:
        return data
    icc = image.info.get("icc_profile")
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    out = io.BytesIO()
    rgba.resize(size, Image.Resampling.LANCZOS).save(out, "PNG", optimize=True, icc_profile=icc)
    return out.getvalue()


def write_zip(entries):
    """A zip of [(name, bytes)] in that order, deflated, with a fixed time: the same input gives the same bytes on
    one machine."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = UNIX
            info.external_attr = ZIP_MODE
            z.writestr(info, data)
    return out.getvalue()


def scale_images(name, data, fx, fy):
    return write_zip([(entry, scale_image("%s: %s" % (name, entry), png, fx, fy))
                      for entry, png in read_entries(name, data)])


# ---- one app --------------------------------------------------------------------------------------------------

def layout_apps(table, keys=None):
    """The apps to work on: those named, or every app with layouts."""
    apps = table["apps"]
    if not keys:
        return [app for app in apps.values() if app["layouts"]]
    out = []
    for key in keys:
        if key not in apps:
            raise Refused("%r is no app in %s: %s" % (key[:80], m.TABLE, ", ".join(apps)))
        if not apps[key]["layouts"]:
            raise Refused("%s has no layout per screen size in %s (it keeps its %s layout)"
                          % (key, m.TABLE, table["base_size"]))
        if apps[key] not in out:
            out.append(apps[key])
    return out


def source_text(app, files):
    folder = app["folder"]
    return "".join("%s  %s/%s\n" % (hashlib.sha256(files[name]).hexdigest(), folder, name)
                   for name in ("%s.xml" % folder, m.image_zip(folder))).encode("ascii")


def generate(root, table, app):
    """{path in nbtapps/layouts/<Folder>/: bytes} of one app's layouts, made from the upstream zip."""
    folder = app["folder"]
    files, _ = m.read_plugin(root, app)
    m.check_plugin(table, app, files)
    xml_name, zip_name = "%s.xml" % folder, m.image_zip(folder)
    out = {"SOURCE": source_text(app, files)}
    for size in table["generated_sizes"]:
        fx, fy = factors(table, size)
        where = "%s/%s/%s/" % (m.LAYOUTS, folder, size)
        xml = scale_xml("%s/%s" % (folder, xml_name), files[xml_name], fx, fy)
        images = scale_images("%s/%s" % (folder, zip_name), files[zip_name], fx, fy)
        m.check_description(where + xml_name, xml)        # what the build checks, checked now
        m.check_image_zip(where + zip_name, images)
        out["%s/%s" % (size, xml_name)] = xml
        out["%s/%s" % (size, zip_name)] = images
    return out


def write_app(root, app, out):
    """Puts the layouts in place as a whole: written to a temporary folder beside the target and moved there. On a
    failure the target is as it was and nothing of the attempt is left."""
    layouts = os.path.join(root, *m.LAYOUTS.split("/"))
    target = os.path.join(layouts, app["folder"])
    if os.path.islink(target) or (os.path.lexists(target) and not os.path.isdir(target)):
        raise Refused("%s/%s is not a folder" % (m.LAYOUTS, app["folder"]))
    os.makedirs(layouts, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".%s-new-" % app["folder"], dir=layouts)
    trash = None
    try:
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(work, 0o777 & ~umask)
        for path, data in out.items():
            full = os.path.join(work, *path.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
        if os.path.lexists(target):
            trash = tempfile.mkdtemp(prefix=".%s-old-" % app["folder"], dir=layouts)
            os.rename(target, os.path.join(trash, app["folder"]))
            try:
                os.rename(work, target)
            except BaseException:
                os.rename(os.path.join(trash, app["folder"]), target)
                raise
        else:
            os.rename(work, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if trash is not None:
            shutil.rmtree(trash, ignore_errors=True)


# ---- --check --------------------------------------------------------------------------------------------------

def committed_paths(folder):
    """Every file and folder under `folder`, as relative paths, folders ending in /."""
    found = []
    for here, dirs, names in os.walk(folder):
        relative = os.path.relpath(here, folder)
        prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
        found += [prefix + d + "/" for d in dirs] + [prefix + n for n in names]
    return found


def compare_images(where, expected, committed):
    """Differences between two image zips: names, order, and each image's size and pixels."""
    try:
        theirs = read_entries(where, committed)
    except Refused as e:
        return [str(e)]
    ours = read_entries(where, expected)
    if [n for n, _ in theirs] != [n for n, _ in ours]:
        missing = sorted(set(n for n, _ in ours) - set(n for n, _ in theirs))
        extra = sorted(set(n for n, _ in theirs) - set(n for n, _ in ours))
        detail = []
        if missing:
            detail.append("lacks %s" % ", ".join(missing[:10]))
        if extra:
            detail.append("holds %s" % ", ".join(extra[:10]))
        return ["%s: the entries are not the base's, in the base's order%s"
                % (where, " (%s)" % "; ".join(detail) if detail else ": the order differs")]
    problems = []
    for (entry, mine), (_, held) in zip(ours, theirs):
        if mine == held:
            continue
        name = "%s: %s" % (where, entry)
        try:
            a, b = decode(name, mine), decode(name, held)
        except Refused as e:
            problems.append(str(e))
            continue
        if a.size != b.size:
            problems.append("%s is %dx%d, not %dx%d" % (name, b.width, b.height, a.width, a.height))
        elif a.convert("RGBA").tobytes() != b.convert("RGBA").tobytes():
            problems.append("%s: its pixels differ" % name)
    return problems


def check_app(root, app, out):
    """[difference] between the layouts generated in memory and those committed."""
    folder = os.path.join(root, *m.LAYOUTS.split("/"), app["folder"])
    where = "%s/%s" % (m.LAYOUTS, app["folder"])
    if not os.path.isdir(folder):
        return ["%s is not there" % where]
    expected = set(out) | {path.rsplit("/", 1)[0] + "/" for path in out if "/" in path}
    held = committed_paths(folder)
    problems = ["%s/%s is not there" % (where, p) for p in sorted(expected - set(held))]
    problems += ["%s/%s is there and should not be" % (where, p) for p in sorted(set(held) - expected)]
    for path, data in out.items():
        full = os.path.join(folder, *path.split("/"))
        if path not in held or not os.path.isfile(full):
            continue
        if os.path.islink(full):
            problems.append("%s/%s is a link" % (where, path))
            continue
        with open(full, "rb") as f:
            committed = f.read()
        if path.endswith(".zip"):
            problems += compare_images("%s/%s" % (where, path), data, committed)
        elif committed != data:
            problems.append("%s/%s differs from what the generator makes" % (where, path))
    return problems


def strays(root, table):
    """Anything in nbtapps/layouts/ that is not a layout app's folder."""
    layouts = os.path.join(root, *m.LAYOUTS.split("/"))
    if not os.path.isdir(layouts):
        return []
    wanted = {app["folder"] for app in table["apps"].values() if app["layouts"]}
    return ["%s/%s is there and is no layout app's folder" % (m.LAYOUTS, name)
            for name in sorted(set(os.listdir(layouts)) - wanted)]


# ---- command line ---------------------------------------------------------------------------------------------

def run(root, keys, check, log=print):
    """Generates (or checks) the apps named, or every layout app. Returns the differences --check found."""
    pillow()
    from PIL import __version__ as version
    table = m.load_table(root)
    apps = layout_apps(table, keys)
    log("Pillow %s" % version)
    problems = []
    for app in apps:
        out = generate(root, table, app)
        if check:
            found = check_app(root, app, out)
            problems += found
            log("%s: %s" % (app["folder"], "%d difference(s)" % len(found) if found else
                            "the committed layouts are what the generator makes"))
        else:
            write_app(root, app, out)
            log("%s: wrote %s/%s/ (%s)" % (app["folder"], m.LAYOUTS, app["folder"],
                                           ", ".join(["SOURCE"] + list(table["generated_sizes"]))))
    if check and not keys:
        found = strays(root, table)
        problems += found
    return problems


def main(argv=None, root=m.ROOT):
    parser = argparse.ArgumentParser(prog="make_layouts.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("apps", nargs="*", metavar="APP", help="an app's key in apps.json (default: every app "
                                                               "with layouts)")
    parser.add_argument("--check", action="store_true",
                        help="compare what is committed with what the generator makes, and write nothing")
    args = parser.parse_args(argv)
    try:
        problems = run(root, args.apps, args.check)
    except Refused as e:
        sys.exit("make_layouts: %s" % e)
    except OSError as e:
        sys.exit("make_layouts: %s" % e)
    if problems:
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        sys.exit("make_layouts: --check found %d difference(s)" % len(problems))


if __name__ == "__main__":
    main()
