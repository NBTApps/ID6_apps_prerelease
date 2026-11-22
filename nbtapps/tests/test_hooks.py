# SPDX-License-Identifier: MIT
"""Tests of the hook scripts nbtapps/hooks/select-layout.sh.in and remove-layout.sh.in. Standard library only.

  python3 -m unittest discover -s nbtapps/tests

The templates are stamped with make_nbtpkg.stamp_hooks and run as the NBTApps agent runs a hook: `<shell> <script>`
from a folder of their own, with only the agent's variables in the environment. PATH holds a folder with the tools
a head unit has that the scripts use, a fake LayerManagerControl written for each case, and a stand-in for each
tool a head unit lacks, which records that it was called. The plugin folder is a temporary one.

The shell is `sh` from this machine's PATH. HOOK_SHELL names another (a command line, such as "zsh --emulate sh");
the head unit's /bin/ksh is not something these tests can run.
"""
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NBTAPPS_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(NBTAPPS_DIR)
sys.path.insert(0, NBTAPPS_DIR)
import make_nbtpkg as m  # noqa: E402

SHELL = shlex.split(os.environ.get("HOOK_SHELL") or "sh")
SHELL[0] = shutil.which(SHELL[0]) or SHELL[0]

TABLE = m.load_table(REPO_ROOT)
APP = TABLE["apps"]["engine-fault-codes"]   # an app with a layout of every size and nothing it writes
FOLDER = APP["folder"]
XML, ZIP = "%s.xml" % FOLDER, "%s_ALL_Images.zip" % FOLDER
BASE = TABLE["base_size"]
SIZES = m.all_sizes(TABLE)
LAYOUT_APPS = [app for app in TABLE["apps"].values() if app["layouts"]]
SCALED = "the screen is %s, to which the head unit scales the " + TABLE["base_size"] + " layout"
SAVING_APP = TABLE["apps"]["hud-gauges"]   # an app without layouts that saves a file in its own folder

# What the scripts may call. The tools are the ones a probe found on a head unit that the brief allows; the rest
# are builtins of the head unit's ksh (pdksh) that sh has too.
TOOLS = ("sed", "cp", "mv", "rm", "sleep", "cat", "grep", "ls")
USED_TOOLS = ("sed", "cp", "mv", "rm", "sleep")
BUILTINS = {"echo", "kill", "wait", "exit", "set", "shift", "umask", "[", "test", ":", "break", "continue",
            "return", "true", "false", "read", "cd", "unset", "export", "trap"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "while", "until", "do", "done", "!", "{", "}", "esac"}
# Tools a head unit does not have (and printf, which its ksh does not have as a builtin either).
LACKING = ("awk", "tr", "head", "tail", "printf", "expr", "basename", "dirname", "touch", "rmdir", "env", "od",
           "cksum", "tar", "id", "seq", "stat", "mktemp", "readlink", "realpath", "timeout", "xargs", "wc", "cut",
           "sort", "find", "date", "which", "print", "pkill", "pgrep", "ps", "bash", "dash")
# Shell features that the head unit's ksh lacks, or that sh and ksh do differently.
CONSTRUCTS = [(r"\[\[", "[["), (r"\$'", "$'...'"), (r"\blocal\b", "local"), (r"\bfunction\b", "function"),
              (r"<<<", "<<<"), (r"\$\(\(", "$((...))"), (r"\$\{[A-Za-z_]+:[0-9-]", "${var:offset}"),
              (r"\b[A-Za-z_]+\[[^]]*\]=", "an array"), (r"\$\{[A-Za-z_]+\[", "an array"), (r"\bsource\b", "source"),
              (r"\btypeset\b", "typeset"), (r"\bdeclare\b", "declare"), (r"(^|[;&|]\s*)select\s", "select"),
              (r" == ", "=="), (r"&>", "&>"), (r"\becho -[neE]", "echo with an option")]

HOOK_TIMEOUT = 30
STATUS_UNSET = object()


def stamped(app=APP):
    return {name: data.decode("utf-8") for name, data in m.stamp_hooks(REPO_ROOT, TABLE, app).items()}


def write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.chmod(path, mode)


def alive(pid):
    """Whether the process is there and not a zombie."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


# ---- the static check of what the scripts call ----------------------------------------------------------------

def _strip_quotes(line):
    """Each quoted string becomes Q; "$name" alone becomes $name, so that a command run from a variable shows. A
    comment goes."""
    out, at = [], 0
    while at < len(line):
        c = line[at]
        if c == "#" and (at == 0 or line[at - 1].isspace()):
            break
        if c == "\\":
            out.append(line[at:at + 2])
            at += 2
        elif c == "'":
            end = line.index("'", at + 1)
            out.append("Q")
            at = end + 1
        elif c == '"':
            end = at + 1
            while line[end] != '"':
                end += 2 if line[end] == "\\" else 1
            inner = line[at + 1:end]
            out.append(inner if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", inner) else "Q")
            at = end + 1
        else:
            out.append(c)
            at += 1
    return "".join(out)


def _substitutions(line):
    """(line with each $(...) as Q, [the text inside each])."""
    inner = []
    while "$(" in line:
        start = line.index("$(")
        depth, at = 1, start + 2
        while depth:
            depth += {"(": 1, ")": -1}.get(line[at], 0)
            at += 1
        inner.append(line[start + 2:at - 1])
        line = line[:start] + "Q" + line[at:]
    return line, inner


CASE_PATTERN = re.compile(r"\s*\(?\s*[^\s()|;]+(?:\s*\|\s*[^\s()|;]+)*\s*\)")


def commands(text):
    """The words in command position in a shell script written in the style of the hooks, and the functions it
    defines. Not a shell parser: it knows the constructs the hooks use, and fails on what it cannot place."""
    text = re.sub(r"\\\n", " ", text)
    found, functions = [], set()
    stack = []      # one entry per open case: True while a pattern is expected
    lines = [line for line in text.split("\n")]
    queue = list(lines)
    while queue:
        raw = queue.pop(0)
        line = _strip_quotes(raw)
        line, inner = _substitutions(line)
        queue[0:0] = inner
        line = re.sub(r"\d*[<>]&\d", " ", line)
        line = re.sub(r"\d*(?:>>|>|<)\s*\S+", " ", line)
        definition = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{?\s*$", line)
        if definition:
            functions.add(definition.group(1))
            continue
        while line.strip():
            if stack and stack[-1]:
                pattern = CASE_PATTERN.match(line)
                if pattern and not re.match(r"\s*esac\b", line):
                    line = line[pattern.end():]
                    stack[-1] = False
                    continue
            parts = re.split(r"(;;|&&|\|\||;|\||&)", line, maxsplit=1)
            segment = parts[0]
            separator = parts[1] if len(parts) > 1 else ""
            line = parts[2] if len(parts) > 2 else ""
            words = segment.split()
            while words and words[0] in KEYWORDS:
                if words[0] == "esac":
                    stack.pop()
                words = words[1:]
            while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]):
                words = words[1:]
            if words and words[0] == "case":
                if words[-1] != "in":
                    raise ValueError("cannot read %r" % raw)
                stack.append(True)
            elif words and words[0] == "for":
                if "in" not in words:
                    raise ValueError("cannot read %r" % raw)
            elif words:
                found.append(words[0])
            if separator == ";;":
                stack[-1] = True
    return found, functions


# What the real table says each app writes outside its folder (see nbtapps/README.md).
WRITTEN = {
    "boostdeck": ["/var/opt/hmi/data/ID5/BoostDeck.prefs", "/var/opt/hmi/data/ID5/BoostDeck.signals"],
    "shiftdeck": ["/var/opt/hmi/data/ID5/ShiftDeck.prefs"],
    "launchdeck": ["/var/opt/hmi/data/ID5/LaunchDeck.prefs", "/var/opt/hmi/data/ID5/LaunchDeck.records",
                   "/var/opt/hmi/data/ID5/LaunchDeck.60-130", "/var/opt/hmi/data/ID5/LaunchDeck.0-100",
                   "/var/opt/hmi/data/ID5/LaunchDeck.100-200"],
}

# What an earlier version of an app saved and this one no longer does, which the preRemove hook removes too:
# HUD Gauges up to 0.1.1 (upstream v0.7.4) saved whether it was on.
WRITTEN_BEFORE = {
    "hud-gauges": ["/var/opt/hmi/data/ID5/Plugins/HudTest/hud-enabled"],
}


class StaticTests(unittest.TestCase):
    def test_every_layout_app_stamps(self):
        self.assertEqual([app["key"] for app in LAYOUT_APPS],
                         ["boostdeck", "shiftdeck", "engine-fault-codes", "mhd-suite", "bootmod3-suite", "launchdeck",
                          "warmdeck", "hybriddeck", "mg-flasher"])
        for app in LAYOUT_APPS:
            with self.subTest(app=app["key"]):
                hooks = stamped(app)
                self.assertEqual(sorted(hooks), ["remove-layout.sh", "select-layout.sh"])
                for name, text in hooks.items():
                    self.assertEqual(m.PLACEHOLDER.findall(text), [], name)
                    self.assertIn("folder=%s\n" % app["folder"], text)
                self.assertIn("base=%s\n" % BASE, hooks["select-layout.sh"])
                self.assertIn("        %s)\n" % "|".join(m.layout_sizes(TABLE, app)), hooks["select-layout.sh"])
                base_at, written = app.get("base_layout_at", []), app.get("written", [])
                self.assertIn("        %s)\n" % ("|".join(base_at) or "none"), hooks["select-layout.sh"])
                self.assertIn("        1440x540|1920x720)\n", hooks["select-layout.sh"])
                self.assertIn('written="%s"\n' % " ".join(written), hooks["remove-layout.sh"])
                self.assertEqual(base_at, [])
                self.assertEqual(m.layout_sizes(TABLE, app), ["1280x480", "800x480", "960x480"])
                self.assertEqual(written, WRITTEN.get(app["key"], []))

    def test_syntax(self):
        work = tempfile.mkdtemp(prefix="hooks-syntax-")
        self.addCleanup(shutil.rmtree, work, True)
        for app in LAYOUT_APPS:
            for name, text in stamped(app).items():
                with self.subTest(app=app["key"], script=name):
                    path = os.path.join(work, "%s-%s" % (app["key"], name))
                    write(path, text)
                    result = subprocess.run(SHELL + ["-n", path], capture_output=True, text=True, timeout=30)
                    self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_commands_a_head_unit_has(self):
        allowed = set(TOOLS) | BUILTINS
        for name, text in stamped().items():
            with self.subTest(script=name):
                found, functions = commands(text)
                strange = sorted(set(found) - allowed - functions - {"$lmc"})
                self.assertEqual(strange, [], "%s calls what a head unit may not have" % name)
                self.assertTrue(set(found) & {"rm"})
                if name == "select-layout.sh":
                    self.assertTrue({"sed", "cp", "mv", "sleep", "kill", "wait", "$lmc"} <= set(found))
                    self.assertEqual(functions, {"tidy", "failed"})
                    self.assertIn('lmc=$d/LayerManagerControl', text)

    def test_constructs(self):
        for name, text in stamped().items():
            code = "\n".join(_strip_quotes(line) for line in text.split("\n"))
            for pattern, what in CONSTRUCTS:
                with self.subTest(script=name, construct=what):
                    self.assertIsNone(re.search(pattern, code), "%s uses %s" % (name, what))

    def test_only_hud_gauges_saves_without_layouts(self):
        self.assertEqual([app["key"] for app in TABLE["apps"].values()
                          if not app["layouts"] and m.saved_files(app)], ["hud-gauges"])
        for app in TABLE["apps"].values():
            with self.subTest(app=app["key"]):
                self.assertEqual(app.get("written_before"), WRITTEN_BEFORE.get(app["key"]))
                self.assertEqual(sorted(stamped(app)), ["remove-layout.sh", "select-layout.sh"] if app["layouts"]
                                 else ["remove-saved.sh"] if m.saved_files(app) else [])

    def test_remove_saved_stamps(self):
        hooks = stamped(SAVING_APP)
        text = hooks["remove-saved.sh"]
        self.assertEqual(m.PLACEHOLDER.findall(text), [])
        self.assertIn("folder=HudTest\n", text)
        self.assertIn('written="%s"\n' % " ".join(WRITTEN_BEFORE["hud-gauges"]), text)
        self.assertNotIn("written", SAVING_APP)
        self.assertEqual(SAVING_APP["written_before"], WRITTEN_BEFORE["hud-gauges"])
        work = tempfile.mkdtemp(prefix="hooks-syntax-")
        self.addCleanup(shutil.rmtree, work, True)
        path = os.path.join(work, "remove-saved.sh")
        write(path, text)
        result = subprocess.run(SHELL + ["-n", path], capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        found, functions = commands(text)
        self.assertEqual((sorted(set(found)), functions), (["[", "echo", "exit", "rm"], set()))
        code = "\n".join(_strip_quotes(line) for line in text.split("\n"))
        for pattern, what in CONSTRUCTS:
            with self.subTest(construct=what):
                self.assertIsNone(re.search(pattern, code), "remove-saved.sh uses %s" % what)

    def test_the_scanner(self):
        """The scanner finds what it is there to find."""
        found, functions = commands("a() {\n  awk 1 x\n}\nif [ -n \"$x\" ]; then y=$(tr a b); fi\n"
                                    "case $x in\n  1 | 2) head -1 ;;\n  *) \"$z\" ;;\nesac\nx=1 tail -f >&2 & a\n")
        self.assertEqual(found, ["awk", "[", "tr", "head", "$z", "tail", "a"])
        self.assertEqual(functions, {"a"})


# ---- running the scripts --------------------------------------------------------------------------------------

def screen_says(text, status=0):
    """A fake LayerManagerControl that prints `text` (str or bytes) and exits with `status`."""
    return {"output": text.encode() if isinstance(text, str) else text, "status": status}


class HookCase(unittest.TestCase):
    """A plugin folder as the package places it, the stamped hooks, and a PATH of tools."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hooks-test-")
        self.addCleanup(self._cleanup)
        self.vp = os.path.join(self.root, "var-plugins")
        self.dir = os.path.join(self.vp, FOLDER)
        for size in SIZES:
            self.put_layout(size)
        self.bin = os.path.join(self.root, "bin")
        os.makedirs(self.bin)
        for tool in TOOLS:
            os.symlink(shutil.which(tool), os.path.join(self.bin, tool))
        self.called = os.path.join(self.root, "lacking-tools-called")
        for tool in LACKING:
            write(os.path.join(self.bin, tool), "#!/bin/sh\necho %s >> '%s'\n" % (tool, self.called), 0o755)
        self.hooks = os.path.join(self.root, "unpacked")
        self.use(APP)
        self.lmc_log = os.path.join(self.root, "lmc-called")

    def use(self, app):
        """Puts the hooks stamped for `app` (APP, or APP with other values) where run_hook runs them from."""
        for name, text in stamped(app).items():
            write(os.path.join(self.hooks, "hooks", name), text)

    def _cleanup(self):
        for top, dirs, _ in os.walk(self.root):
            for d in dirs:
                os.chmod(os.path.join(top, d), 0o755)
        shutil.rmtree(self.root, ignore_errors=True)

    def put_layout(self, size, content=None):
        content = content or size
        write(os.path.join(self.dir, "layouts", size, XML), "<pluginApps size='%s'/>\n" % content)
        write(os.path.join(self.dir, "layouts", size, ZIP), b"PK images of " + content.encode())

    def layout(self, size):
        return self.read(os.path.join("layouts", size, XML)), self.read(os.path.join("layouts", size, ZIP))

    def active(self):
        return self.read(XML), self.read(ZIP)

    def read(self, relative):
        path = os.path.join(self.dir, relative)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def snapshot(self):
        """{path in the plugin folder: (bytes, mode)}, folders as (None, mode)."""
        out = {}
        if not os.path.isdir(self.dir):
            return out
        for top, dirs, files in os.walk(self.dir):
            for name in dirs + files:
                path = os.path.join(top, name)
                relative = os.path.relpath(path, self.dir)
                mode = stat.S_IMODE(os.lstat(path).st_mode)
                if os.path.isdir(path):
                    out[relative] = (None, mode)
                else:
                    with open(path, "rb") as f:
                        out[relative] = (f.read(), mode)
        return out

    def fake_lmc(self, output=b"", status=0, hang=False, pidfile=None, executable=True):
        """A LayerManagerControl in PATH that logs how it was called, prints `output` and exits with `status`, or
        hangs after printing it."""
        data = os.path.join(self.root, "lmc-output")
        write(data, output)
        lines = ["#!/bin/sh",
                 "echo \"LD_LIBRARY_PATH=$LD_LIBRARY_PATH args=$*\" >> '%s'" % self.lmc_log]
        if pidfile:
            lines.append("echo $$ > '%s'" % pidfile)
        lines.append("'%s' '%s'" % (shutil.which("cat"), data))
        if hang:
            lines.append("exec '%s' 60" % shutil.which("sleep"))
        lines.append("exit %d" % status)
        write(os.path.join(self.bin, "LayerManagerControl"), "\n".join(lines) + "\n", 0o755 if executable else 0o644)

    def run_hook(self, script, action="install", plugins_dir=STATUS_UNSET, umask=0o022):
        env = {"PATH": self.bin, "NBT_ACTION": action, "NBT_PKG_ID": APP["id"], "NBT_PKG_VERSION": "1.0.0",
               "NBT_APP_DIR": os.path.join(self.root, "app"), "NBT_DATA_DIR": os.path.join(self.root, "data"),
               "NBT_TMP_DIR": os.path.join(self.root, "tmp-not-made")}
        if action == "upgrade":
            env["NBT_OLD_VERSION"] = "0.9.0"
        if plugins_dir is STATUS_UNSET:
            env["NBT_VAR_PLUGINS_DIR"] = self.vp
        elif plugins_dir is not None:
            env["NBT_VAR_PLUGINS_DIR"] = plugins_dir
        start = time.monotonic()
        result = subprocess.run(SHELL + ["hooks/" + script], cwd=self.hooks, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=HOOK_TIMEOUT,
                                umask=umask)
        result.elapsed = time.monotonic() - start
        result.text = result.stdout.decode("utf-8", "replace")
        return result

    def select(self, action="install", **kwargs):
        """Runs select-layout.sh and checks what holds whatever happens: nothing but the two active files changes
        in the plugin folder, nothing of the script's own is left there, and no tool a head unit lacks is
        called."""
        before = self.snapshot()
        result = self.run_hook("select-layout.sh", action, **kwargs)
        after = self.snapshot()
        changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
        self.assertLessEqual(changed, {XML, ZIP}, result.text)
        self.assertEqual([p for p in after if p.startswith(".")], [], result.text)
        self.assert_no_lacking_tool()
        self.assertNotIn("not found", result.text)
        self.assertLess(result.elapsed, 10, result.text)
        return result

    def assert_no_lacking_tool(self):
        if os.path.exists(self.called):
            with open(self.called) as f:
                self.fail("the script called %s, which a head unit does not have" % ", ".join(f.read().split()))

    def assert_chosen(self, result, size, fallback=None):
        self.assertEqual(result.returncode, 0, result.text)
        self.assertEqual(self.active(), self.layout(size))
        for name in (XML, ZIP):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, name)).st_mode), 0o644)
        lines = result.text.splitlines()
        self.assertEqual(lines[-1], "%s: the %s layout is in place; the HMI uses it from its next start"
                         % (FOLDER, size))
        if fallback is None:
            self.assertEqual(len(lines), 1, result.text)
        else:
            self.assertEqual(lines[:-1], ["%s: %s; using the %s layout" % (FOLDER, fallback, BASE)])

    def assert_failed(self, result, needle):
        self.assertEqual(result.returncode, 1, result.text)
        self.assertIn(needle, result.text)
        self.assertNotIn("is in place", result.text)


class SelectTests(HookCase):
    def test_each_size(self):
        for size in SIZES:
            with self.subTest(size=size):
                x, y = size.split("x")
                self.fake_lmc(b"Screen 0:\n- resolution: x=%s, y=%s\n" % (x.encode(), y.encode()))
                self.assert_chosen(self.select(), size)
        with open(self.lmc_log) as f:
            calls = f.read().splitlines()
        self.assertEqual(calls, ["LD_LIBRARY_PATH=/opt/sys/lib:/usr/lib:/lib args=get screen 0"] * len(SIZES))

    def test_output_forms(self):
        cases = [
            ("Screen 0 info:\n- ID: 0\n- resolution: x=1920, y=720\n- layer render order: 3000, 3001\n", "1920x720"),
            ("RESOLUTION:X=960,Y=480\n", "960x480"),
            ("  Resolution:   x   =   1440,    Y =  540   \n", "1440x540"),
            ("resolution:\tx\t=\t800,\ty\t=\t480\n", "800x480"),
            ("resolution: x=1920, y=720\r\nother: 1\r\n", "1920x720"),
            ("screen 0: resolution: x=800, y=480 (native)", "800x480"),             # no newline at the end
            ("resolution: x=800, y=480\nresolution: x=1920, y=720\n", "800x480"),   # the first line counts
            ("resolution: x=12800, y=480\nresolution: x=960, y=480\n", "960x480"),  # x of 5 digits: no match
            ("resolution: x=1280, y=4805\n", None),                                 # y: its first 4 digits
            ("resolution: x=1280 , y=480\nresolution: x=1440, y=540\n", "1440x540"),
            ("\x00\xffbinary\nresolution: x=1440, y=540\n", "1440x540"),
        ]
        for text, size in cases:
            with self.subTest(text=text):
                self.fake_lmc(text.encode("latin-1"))
                result = self.select()
                if size is None:
                    self.assert_chosen(result, BASE, "the screen is 1280x4805, a size this app has no layout for")
                elif size in TABLE["base_shown_at"]:
                    self.assert_chosen(result, BASE, SCALED % size)
                else:
                    self.assert_chosen(result, size)

    def test_base_shown_at(self):
        """An 8:3 screen other than the base size gets the base layout, which the head unit scales to it, also
        when a layout of that size from an older version of the package is still there."""
        self.assertEqual(TABLE["base_shown_at"], ["1440x540", "1920x720"])
        for size in TABLE["base_shown_at"]:
            with self.subTest(size=size):
                x, y = size.split("x")
                self.fake_lmc(b"resolution: x=%s, y=%s\n" % (x.encode(), y.encode()))
                self.assert_chosen(self.select(), BASE, SCALED % size)
                self.put_layout(size)
                self.assert_chosen(self.select("upgrade"), BASE, SCALED % size)

    def test_unknown_size(self):
        self.fake_lmc(b"- resolution: x=1024, y=600\n")
        self.assert_chosen(self.select(), BASE, "the screen is 1024x600, a size this app has no layout for")

    def test_nothing_usable(self):
        cases = [
            b"",
            b"garbage\n\x01\x02\xfe\n" * 50,
            b"resolution: x=1280 y=480\n",          # no comma
            b"resolution: x=1, y=480\n",            # one digit
            b"resolution: x=1280, y=4\n",
            b"resolution x=1280, y=480\n",          # no colon
            b"x=1280, y=480\n",
            b"resolution: w=1280, h=480\n",
            b"resolution:\nx=1280, y=480\n",        # a line at a time
            b"resolution: x=1280,\ny=480\n",
        ]
        for output in cases:
            with self.subTest(output=output[:40]):
                self.fake_lmc(output)
                self.assert_chosen(self.select(), BASE, "LayerManagerControl gave no screen resolution")

    def test_failing_command(self):
        self.fake_lmc(b"", status=3)
        self.assert_chosen(self.select(), BASE, "LayerManagerControl gave no screen resolution (it exited with 3)")
        self.fake_lmc(b"error: cannot connect to the layer manager\n", status=1)
        self.assert_chosen(self.select(), BASE, "LayerManagerControl gave no screen resolution (it exited with 1)")

    def test_no_command(self):
        result = self.select()
        self.assert_chosen(result, BASE, "LayerManagerControl is not on this head unit's PATH")
        self.assertLess(result.elapsed, 1)
        self.fake_lmc(b"resolution: x=800, y=480\n", executable=False)
        self.assert_chosen(self.select(), BASE, "LayerManagerControl is not on this head unit's PATH")

    def test_hang(self):
        for output in (b"", b"resolution: x=800, y=480\n"):
            with self.subTest(output=output):
                pidfile = os.path.join(self.root, "lmc.pid")
                self.fake_lmc(output, hang=True, pidfile=pidfile)
                result = self.select()
                self.assert_chosen(result, BASE, "LayerManagerControl did not answer within 5 seconds and was "
                                                 "stopped")
                self.assertGreater(result.elapsed, 4.5)
                self.assertLess(result.elapsed, 10)
                with open(pidfile) as f:
                    pid = int(f.read())
                deadline = time.monotonic() + 2
                while alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(alive(pid), "the fake LayerManagerControl is still running")

    def test_base_layout_at(self):
        """At a size in base_layout_at the package holds no layout, and the base one is shown."""
        self.use(dict(APP, base_layout_at=["800x480"]))
        shutil.rmtree(os.path.join(self.dir, "layouts", "800x480"))
        for size in SIZES:
            with self.subTest(size=size):
                x, y = size.split("x")
                self.fake_lmc(b"resolution: x=%s, y=%s\n" % (x.encode(), y.encode()))
                if size == "800x480":
                    self.assert_chosen(self.select(), BASE, "the screen is %s, and this app's layout of that size "
                                                            "is reported not to fit it" % size)
                else:
                    self.assert_chosen(self.select(), size)
        # A layout of that size left by an older version of the package is not used either.
        self.put_layout("800x480")
        self.fake_lmc(b"resolution: x=800, y=480\n")
        self.assert_chosen(self.select("upgrade"), BASE, "the screen is 800x480, and this app's layout of that "
                                                         "size is reported not to fit it")

    def test_known_size_without_its_layout(self):
        shutil.rmtree(os.path.join(self.dir, "layouts", "800x480"))
        self.fake_lmc(b"resolution: x=800, y=480\n")
        self.assert_chosen(self.select(), BASE, "the screen is 800x480, but layouts/800x480/%s is not there" % XML)
        os.remove(os.path.join(self.dir, "layouts", "960x480", ZIP))
        self.fake_lmc(b"resolution: x=960, y=480\n")
        self.assert_chosen(self.select(), BASE, "the screen is 960x480, but layouts/960x480/%s is not there" % ZIP)
        os.remove(os.path.join(self.dir, "layouts", "960x480", XML))
        self.fake_lmc(b"resolution: x=960, y=480\n")
        self.assert_chosen(self.select(), BASE, "the screen is 960x480, but layouts/960x480/%s is not there" % XML)

    def test_plugins_dir_not_given(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.fake_lmc(b"resolution: x=800, y=480\n")
                result = self.select(plugins_dir=value)
                self.assert_failed(result, "NBT_VAR_PLUGINS_DIR is not set")
                self.assertEqual(self.active(), (None, None))
        self.assertFalse(os.path.exists(self.lmc_log))

    def test_base_layout_missing(self):
        for name in (XML, ZIP):
            with self.subTest(name=name):
                self.put_layout(BASE)
                os.remove(os.path.join(self.dir, "layouts", BASE, name))
                self.fake_lmc(b"resolution: x=800, y=480\n")
                self.assert_failed(self.select(), "the %s layout is not in %s/layouts/%s" % (BASE, self.dir, BASE))
                self.assertEqual(self.active(), (None, None))
        shutil.rmtree(self.dir)
        self.assert_failed(self.select(), "the %s layout is not in" % BASE)
        self.assertFalse(os.path.exists(self.dir))

    def test_leftovers_of_a_stopped_run_go(self):
        for name in (".select-layout.screen", ".select-layout.xml", ".select-layout.zip", ".select-layout.xml.old"):
            write(os.path.join(self.dir, name), "left\n")
        self.fake_lmc(b"resolution: x=800, y=480\n")
        result = self.run_hook("select-layout.sh")
        self.assertEqual(result.returncode, 0, result.text)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([XML, ZIP, "layouts"]))

    def test_second_run_renames_over(self):
        """An upgrade writes over the active files, and writes into neither: a reader that has one open keeps
        reading the old one."""
        self.fake_lmc(b"resolution: x=800, y=480\n")
        self.assert_chosen(self.select(), "800x480")
        with open(os.path.join(self.dir, XML), "rb") as held_xml, open(os.path.join(self.dir, ZIP), "rb") as held_zip:
            self.put_layout("800x480", "800x480 of version 2")
            result = self.select("upgrade")
            self.assert_chosen(result, "800x480")
            self.assertEqual(self.active()[0], b"<pluginApps size='800x480 of version 2'/>\n")
            self.assertEqual(held_xml.read(), b"<pluginApps size='800x480'/>\n")
            self.assertEqual(held_zip.read(), b"PK images of 800x480")
        self.fake_lmc(b"resolution: x=1920, y=720\n")
        self.assert_chosen(self.select("upgrade"), BASE, SCALED % "1920x720")
        self.fake_lmc(b"", status=1)
        self.assert_chosen(self.select("upgrade"), BASE, "LayerManagerControl gave no screen resolution "
                                                         "(it exited with 1)")

    def test_mode_whatever_the_umask(self):
        self.fake_lmc(b"resolution: x=800, y=480\n")
        self.assert_chosen(self.select(umask=0o077), "800x480")

    def test_owner_file_left_alone(self):
        write(os.path.join(self.dir, "gateway-ip.txt"), "169.254.21.3\n")
        self.fake_lmc(b"resolution: x=800, y=480\n")
        self.assert_chosen(self.select(), "800x480")
        self.assertEqual(self.read("gateway-ip.txt"), b"169.254.21.3\n")

    def test_active_path_is_a_folder(self):
        os.makedirs(os.path.join(self.dir, ZIP))
        self.fake_lmc(b"resolution: x=800, y=480\n")
        self.assert_failed(self.select("upgrade"), "is a folder")
        self.assertTrue(os.path.isdir(os.path.join(self.dir, ZIP)))


class FailureTests(HookCase):
    """A copy or a rename that fails. A stand-in for cp or mv fails where it is told to, and is the real tool
    elsewhere."""

    OLD = (b"<pluginApps size='old'/>\n", b"PK old images")

    def setUp(self):
        super().setUp()
        self.fake_lmc(b"resolution: x=800, y=480\n")

    def place_old(self):
        write(os.path.join(self.dir, XML), self.OLD[0])
        write(os.path.join(self.dir, ZIP), self.OLD[1])

    def failing(self, tool, when):
        """`tool` exits with 1 when its last argument ends with `when`."""
        real = shutil.which(tool)
        os.remove(os.path.join(self.bin, tool))
        write(os.path.join(self.bin, tool), "#!/bin/sh\nfor last in \"$@\"; do :; done\ncase $last in\n"
                                            "  *%s) echo \"%s: no room\" >&2; exit 1 ;;\nesac\nexec '%s' \"$@\"\n"
                                            % (when, tool, real), 0o755)

    def test_copy_fails(self):
        for when, needle, actions in ((".select-layout.xml", "cannot copy the 800x480 %s" % XML, ("install", "upgrade")),
                                      (".select-layout.zip", "cannot copy the 800x480 %s" % ZIP, ("install", "upgrade")),
                                      (".select-layout.xml.old", "cannot keep a copy of", ("upgrade",))):
            for action in actions:
                with self.subTest(when=when, action=action):
                    self.failing("cp", when)
                    self.place_old()
                    result = self.select(action)
                    self.assert_failed(result, needle)
                    if action == "install":
                        self.assertEqual(self.active(), (None, None))
                    else:
                        self.assertEqual(self.active(), self.OLD)

    def test_rename_fails(self):
        for when, needle in ((XML, "cannot rename the new %s into place" % XML),
                             (ZIP, "cannot rename the new %s into place" % ZIP)):
            for action in ("install", "upgrade"):
                with self.subTest(when=when, action=action):
                    self.failing("mv", "/" + when)
                    self.place_old()
                    result = self.select(action)
                    self.assert_failed(result, needle)
                    if action == "install":
                        self.assertEqual(self.active(), (None, None))
                    else:
                        self.assertEqual(self.active(), self.OLD)

    def test_rename_of_the_zip_fails_with_no_old_files(self):
        self.failing("mv", "/" + ZIP)
        self.assert_failed(self.select("upgrade"), "cannot rename")
        self.assertEqual(self.active(), (None, None))

    @unittest.skipIf(os.geteuid() == 0, "root writes into a read-only folder")
    def test_read_only_folder(self):
        for action in ("install", "upgrade"):
            with self.subTest(action=action):
                self.place_old()
                os.chmod(self.dir, 0o555)
                try:
                    result = self.select(action)
                finally:
                    os.chmod(self.dir, 0o755)
                self.assert_failed(result, "no layout placed")
                self.assertEqual(self.active(), self.OLD)   # on an install too: they cannot be removed either
        os.remove(os.path.join(self.dir, XML))
        os.remove(os.path.join(self.dir, ZIP))
        os.chmod(self.dir, 0o555)
        try:
            result = self.select("install")
        finally:
            os.chmod(self.dir, 0o755)
        self.assert_failed(result, "cannot copy the")
        self.assertEqual(sorted(os.listdir(self.dir)), ["layouts"])


class RemoveTests(HookCase):
    def remove(self, **kwargs):
        result = self.run_hook("remove-layout.sh", "remove", **kwargs)
        self.assert_no_lacking_tool()
        self.assertNotIn("not found", result.text)
        return result

    def test_removes_what_select_made(self):
        write(os.path.join(self.dir, "gateway-ip.txt"), "169.254.21.3\n")
        self.fake_lmc(b"resolution: x=800, y=480\n")
        self.assertEqual(self.run_hook("select-layout.sh").returncode, 0)
        for name in (".select-layout.screen", ".select-layout.xml", ".select-layout.zip", ".select-layout.xml.old"):
            write(os.path.join(self.dir, name), "left\n")
        before = self.snapshot()
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.text)
        self.assertEqual(result.text, "%s: the active layout is removed\n" % FOLDER)
        after = self.snapshot()
        gone = sorted(set(before) - set(after))
        self.assertEqual(gone, sorted([XML, ZIP, ".select-layout.screen", ".select-layout.xml", ".select-layout.zip",
                                       ".select-layout.xml.old"]))
        self.assertEqual({p: v for p, v in before.items() if p not in gone}, after)
        self.assertEqual(self.read("gateway-ip.txt"), b"169.254.21.3\n")

    def test_nothing_to_remove(self):
        before = self.snapshot()
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.text)
        self.assertEqual(self.snapshot(), before)
        shutil.rmtree(self.dir)
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.text)
        self.assertFalse(os.path.exists(self.dir))

    def test_plugins_dir_not_given(self):
        write(os.path.join(self.dir, XML), "active\n")
        for value in (None, ""):
            with self.subTest(value=value):
                result = self.remove(plugins_dir=value)
                self.assertEqual(result.returncode, 0, result.text)
                self.assertIn("NBT_VAR_PLUGINS_DIR is not set", result.text)
                self.assertEqual(self.read(XML), b"active\n")

    def saving(self):
        """Stamps the hooks for an app that writes two files outside its folder. Returns their paths."""
        saved = os.path.join(self.root, "saved")
        paths = [os.path.join(saved, "%s.prefs" % FOLDER), os.path.join(saved, "deeper", "state.bin")]
        os.makedirs(os.path.dirname(paths[1]))
        self.use(dict(APP, written=paths))
        return paths

    def test_removes_what_the_app_saved(self):
        prefs, state = self.saving()
        temporary = prefs + ".Ab3x_9"
        others = [prefs + ".bak", prefs + ".1234567", prefs + "2", os.path.join(os.path.dirname(prefs), "Other.prefs")]
        for path in [prefs, temporary] + others:
            write(path, "G\x01\x02\x01")
        write(os.path.join(self.dir, XML), "active\n")
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.text)
        self.assertEqual(result.text.splitlines(), ["%s: removed what the app saved: %s %s" % (FOLDER, prefs, temporary),
                                                    "%s: the active layout is removed" % FOLDER])
        self.assertEqual([p for p in [prefs, temporary, state] if os.path.exists(p)], [])
        self.assertEqual([p for p in others if not os.path.isfile(p)], [])
        self.assertIsNone(self.read(XML))
        self.assertTrue(os.path.isdir(os.path.dirname(state)))      # folders are never removed

    def test_nothing_saved(self):
        self.saving()
        result = self.remove()
        self.assertEqual((result.returncode, result.text), (0, "%s: the active layout is removed\n" % FOLDER))

    def test_saved_files_stay_on_an_upgrade(self):
        prefs, state = self.saving()
        for path in (prefs, state, prefs + ".Ab3x_9"):
            write(path, "kept")
        result = self.run_hook("remove-layout.sh", "upgrade")
        self.assertEqual((result.returncode, result.text), (0, "%s: the active layout is removed\n" % FOLDER))
        self.assertEqual([p for p in (prefs, state, prefs + ".Ab3x_9") if not os.path.isfile(p)], [])

    def test_saved_files_go_without_the_plugins_dir(self):
        prefs, state = self.saving()
        write(state, "x")
        write(os.path.join(self.dir, XML), "active\n")
        result = self.remove(plugins_dir=None)
        self.assertEqual(result.returncode, 0, result.text)
        self.assertEqual(result.text.splitlines()[0], "%s: removed what the app saved: %s" % (FOLDER, state))
        self.assertIn("NBT_VAR_PLUGINS_DIR is not set", result.text)
        self.assertFalse(os.path.exists(state))
        self.assertEqual(self.read(XML), b"active\n")

    def test_a_folder_at_a_saved_path_is_left(self):
        prefs, state = self.saving()
        os.makedirs(os.path.join(prefs, "inside"))
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.text)
        self.assertTrue(os.path.isdir(os.path.join(prefs, "inside")))

    @unittest.skipIf(os.geteuid() == 0, "root writes into a read-only folder")
    def test_cannot_remove_what_the_app_saved(self):
        prefs, state = self.saving()
        write(prefs, "x")
        write(os.path.join(self.dir, XML), "active\n")
        os.chmod(os.path.dirname(prefs), 0o555)
        try:
            result = self.remove()
        finally:
            os.chmod(os.path.dirname(prefs), 0o755)
        self.assertEqual(result.returncode, 1, result.text)
        self.assertIn("could not remove %s" % prefs, result.text)
        self.assertIsNone(self.read(XML))                           # the rest is still removed

    @unittest.skipIf(os.geteuid() == 0, "root writes into a read-only folder")
    def test_cannot_remove(self):
        write(os.path.join(self.dir, XML), "active\n")
        os.chmod(self.dir, 0o555)
        try:
            result = self.remove()
        finally:
            os.chmod(self.dir, 0o755)
        self.assertEqual(result.returncode, 1, result.text)
        self.assertIn("could not remove %s" % os.path.join(self.dir, XML), result.text)


class RemoveSavedTests(HookCase):
    """remove-saved.sh, stamped for HUD Gauges with the file an earlier version saved in a plugin folder of the
    test's own."""

    def setUp(self):
        super().setUp()
        self.hud = os.path.join(self.vp, SAVING_APP["folder"])
        self.saved = os.path.join(self.hud, "hud-enabled")
        for name in ("libHudTest.so", "HudTest.manifest", "HudTest.xml", "hud-shape-qnx"):
            write(os.path.join(self.hud, name), name)
        write(os.path.join(self.hud, "gateway-ip.txt"), "169.254.21.3\n")
        self.use(dict(SAVING_APP, written_before=[self.saved]))

    def remove(self, action="remove", **kwargs):
        result = self.run_hook("remove-saved.sh", action, **kwargs)
        self.assert_no_lacking_tool()
        self.assertNotIn("not found", result.text)
        return result

    def others(self):
        return sorted(n for n in os.listdir(self.hud) if not n.startswith("hud-enabled"))

    def test_removes_what_the_app_saved(self):
        temporary = self.saved + ".Ab3x_9"
        kept = [self.saved + ".bak", self.saved + ".1234567", self.saved + "2"]
        for path in [self.saved, temporary] + kept:
            write(path, "1\n")
        result = self.remove()
        self.assertEqual((result.returncode, result.text),
                         (0, "HudTest: removed what the app saved: %s %s\n" % (self.saved, temporary)))
        self.assertEqual([p for p in (self.saved, temporary) if os.path.exists(p)], [])
        self.assertEqual([p for p in kept if not os.path.isfile(p)], [])
        self.assertEqual(self.others(), ["HudTest.manifest", "HudTest.xml", "gateway-ip.txt", "hud-shape-qnx",
                                         "libHudTest.so"])

    def test_nothing_saved(self):
        result = self.remove()
        self.assertEqual((result.returncode, result.text), (0, ""))
        shutil.rmtree(self.hud)
        result = self.remove()
        self.assertEqual((result.returncode, result.text), (0, ""))

    def test_kept_on_an_upgrade(self):
        write(self.saved, "1\n")
        write(self.saved + ".Ab3x_9", "1\n")
        result = self.remove("upgrade")
        self.assertEqual((result.returncode, result.text), (0, ""))
        self.assertTrue(os.path.isfile(self.saved) and os.path.isfile(self.saved + ".Ab3x_9"))

    def test_without_the_plugins_dir(self):
        write(self.saved, "1\n")
        result = self.remove(plugins_dir=None)
        self.assertEqual(result.returncode, 0, result.text)
        self.assertFalse(os.path.exists(self.saved))

    def test_a_folder_at_the_saved_path_is_left(self):
        os.makedirs(os.path.join(self.saved, "inside"))
        result = self.remove()
        self.assertEqual((result.returncode, result.text), (0, ""))
        self.assertTrue(os.path.isdir(os.path.join(self.saved, "inside")))

    @unittest.skipIf(os.geteuid() == 0, "root writes into a read-only folder")
    def test_cannot_remove(self):
        write(self.saved, "1\n")
        os.chmod(self.hud, 0o555)
        try:
            result = self.remove()
        finally:
            os.chmod(self.hud, 0o755)
        self.assertEqual(result.returncode, 1, result.text)
        self.assertIn("HudTest: could not remove %s" % self.saved, result.text)
        self.assertTrue(os.path.isfile(self.saved))


if __name__ == "__main__":
    unittest.main()
