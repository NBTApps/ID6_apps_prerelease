<!-- SPDX-License-Identifier: MIT -->
# NBTApps packaging

This folder belongs to the NBTApps fork of `inotbarnacle/ID6_apps_prerelease`. Upstream's maintainer agreed to
the apps being packaged for [NBTApps](https://github.com/tom-pryor/nbtapps), an app store for BMW NBT Evo head
units. It turns upstream's prebuilt plugins into signed `.nbtpkg` packages, one per app, which the NBTApps agent
installs on a head unit.

Nothing is compiled here. Each package holds upstream's files from `packages/*.zip` as they are.

The fork's own files are this folder and `.github/workflows/nbtpkg.yml`. Everything else is upstream's and is
never changed here.

## The apps

`apps.json` lists them. Each package's id is `com.github.nbtapps.<key>`.

| Key | Name | Plugin folder | From | Connector id | Layouts |
|---|---|---|---|---|---|
| `boostdeck` | BoostDeck | `BoostDeck` | `packages/dashboard.zip` | 100 | per screen size, 1280×480 at 1440×540 |
| `shiftdeck` | ShiftDeck | `ShiftDeck` | `packages/dashboard.zip` | 104 | per screen size, 1280×480 at 1440×540 |
| `engine-fault-codes` | Engine Fault Codes | `EngineCodes` | `packages/dashboard.zip` | 102 | per screen size |
| `mhd-suite` | MHD+ Suite | `MhdMaps` | `packages/dashboard.zip` | 103 | per screen size |
| `bootmod3-suite` | Bootmod3 Suite | `Bootmod3Suite` | `packages/bootmod3.zip` | 111 | per screen size |
| `xdrive-control` | xDrive Control | `XDriveControl` | `packages/xdrive.zip` | 112 | none (no images) |
| `road-jet` | Road Jet | `RoadJet` | `packages/road-jet.zip` | 217 | 1280×480 only |
| `beamer` | BEAMer | `BEAMer` | `packages/beamer.zip` | 218 | 1280×480 only |

MHD+ Suite and Bootmod3 Suite conflict: a head unit has one or the other. Bootmod3 Suite and xDrive Control also
carry `source/third-party/idrive7-apps-LICENSE`. `packages/hud-gauges.zip` is not packaged.

Every package installs one plugin folder into `${var_plugins}/<Folder>/`. On a car that is
`/var/opt/hmi/data/ID5/Plugins/<Folder>/`. The libraries have that path compiled in to start their worker
programs, so the files must go exactly there. `${var_plugins}` needs an agent that knows it, so each package sets
`requires.agent` to `apps.json`'s `requires_agent`. Every package asks for an HMI restart.

## What a package holds

```
manifest.json, manifest.sig
payload/<Folder>/lib<Folder>.so          -> ${var_plugins}/<Folder>/            mode 0644, the whole folder
payload/<Folder>/<Folder>.manifest
payload/<Folder>/<Folder>.xml            (apps without layouts only)
payload/<Folder>/<Folder>_ALL_Images.zip (apps without layouts, if the app has images)
payload/<Folder>/layouts/<WxH>/...       (apps with layouts: each size the app has a layout for, see below)
payload/bin/<worker>                     -> ${var_plugins}/<Folder>/<worker>    mode 0755, each worker
hooks/select-layout.sh                   postInstall, 30 s (apps with layouts)
hooks/remove-layout.sh                   preRemove, 30 s (apps with layouts)
licenses/LICENSE                         source/LICENSE, not installed
licenses/<name>                          any other licence apps.json names, not installed
```

The licences ride in the archive and nothing installs them. `nbtpkg.py` and the agent both accept files that the
manifest does not install, as they do for hook scripts.

## What `build` checks

Before it builds, `make_nbtpkg.py build` refuses the app unless:

- the zip's `SHA256SUMS`, where it has one, lists every file of the plugin folder with the right hash, and lists
  no file of that folder that is not there (`road-jet.zip` and `beamer.zip` have none, and the build says so);
- the plugin folder holds exactly `lib<Folder>.so`, the workers, `<Folder>.manifest`, `<Folder>.xml` and, for an
  app with images, `<Folder>_ALL_Images.zip`. A file upstream adds later stops the build until this folder is
  updated for it;
- the library and the workers are 32-bit little-endian ARM ELF files (a shared library, and programs);
- the library holds `/var/opt/hmi/data/ID5/Plugins/<Folder>/<worker>` for each worker;
- every absolute path the library or a worker holds as a whole string is one this folder knows: `/dev/null`, a
  path under `/lib/`, `/usr/lib/` or `/opt/sys/lib/`, a path in the plugin's own folder, or a path `apps.json`
  names under the app's `written`; and the library holds each `written` path. A path upstream adds later stops
  the build until someone has read what the plugin does with it (see [Files a plugin
  writes](#files-a-plugin-writes)). The check sees a path that is a string of its own in the file. It does not
  see one the program puts together while it runs;
- `<Folder>.manifest` is XML with no DOCTYPE or ENTITY, and names `<Folder>`, `{pluginDir}/<Folder>`,
  `{pluginDir}/<Folder>.xml`, `{pluginDir}/<Folder>_ALL_Images.zip` (or no image file, for an app without
  images) and exactly the connector id in `apps.json`;
- `<Folder>.xml` is XML with no DOCTYPE or ENTITY and no `entryButtonFocusID`;
- the image zip is a zip whose files all read back.

## Building on your own computer

You need python3, a checkout of NBTApps that has `${var_plugins}` (its `headunit/` folder), and, to sign, the
`cryptography` module.

```sh
export NBTAPPS_HEADUNIT=/path/to/nbtapps/headunit
python3 nbtapps/make_nbtpkg.py list
python3 nbtapps/make_nbtpkg.py build --app road-jet --version 1.0.0 --out nbtapps/out     # not signed
python3 -m unittest discover -s nbtapps/tests
```

`--nbtapps DIR` works in place of `NBTAPPS_HEADUNIT`. `nbtapps/out/` is ignored by git.

A signed build takes the private key from `--key FILE` or from `NBTPKG_AUTHOR_KEY` (64 hex characters), never
both. It needs `nbtapps/author.pub`, or `--pub FILE`, and refuses a key that is not that file's private half. It
verifies the archive with that public key. `--require-key` fails a build that has no key.

A signed build also writes `nbtpkg-record.json` beside the archive. Its `source.commit` is `HEAD`, so a signed
build refuses a tree with changes under `packages/`, `source/` or `nbtapps/` that are not committed. With
`--allow-dirty` it signs such a tree and writes no record. The record names the release
`<key>-v<version>` of `--repo` (default `NBTApps/ID6_apps_prerelease`).

The tests need nothing but python3. The ones that build or sign need `NBTAPPS_HEADUNIT`, and are skipped
without it. They fail if it names a folder that is not an NBTApps `headunit/`. With `NBTCAT_TOOLS` set to the
catalogue repo's `tools/` folder, the end-to-end test also checks each real archive with `nbtcat.py`. The tests
make their own throwaway keys.

## Layouts and hooks

Five apps (`"layouts": true`) need a layout file per screen resolution. Upstream ships the 1280×480 one only.
The rest come from two parts of this folder: `make_layouts.py` generates the layouts, and the hook scripts pick
one on the head unit. `build` refuses one of these five apps when a file of either part is missing, and lists
what is missing. Road Jet and BEAMer ship their 1280×480 layout only, with no hook. This is the contract both
parts keep to.

### Layouts (the layout step)

- The base size is `apps.json`'s `base_size`, 1280x480. Its layout is `<Folder>.xml` and
  `<Folder>_ALL_Images.zip` from the upstream zip. It is not copied into this folder.
- Each other size in `generated_sizes` (800x480, 960x480, 1440x540, 1920x720) is committed as:

  ```
  nbtapps/layouts/<Folder>/<WxH>/<Folder>.xml
  nbtapps/layouts/<Folder>/<WxH>/<Folder>_ALL_Images.zip
  ```

- `nbtapps/layouts/<Folder>/SOURCE` names the base files the layouts were made from, in `sha256sum` format,
  with their paths in the upstream zip:

  ```
  <sha256>  <Folder>/<Folder>.xml
  <sha256>  <Folder>/<Folder>_ALL_Images.zip
  ```

  After `unzip packages/<zip>.zip` in an empty folder, `sha256sum -c` checks it there.
- `build` refuses an app whose `SOURCE` does not match the zip: upstream changed the base layout and the others
  are stale. It also refuses a missing size, a folder or file there that is not listed above, a generated
  `<Folder>.xml` that is not XML or has an `entryButtonFocusID`, and an image zip that does not read back.

`make_layouts.py` makes them, by the rule upstream's own desktop installer follows. For a size W×H, with
fx = W/1280 and fy = H/480:

- in `<Folder>.xml`, each `<property id="20">` (x) and `id="9"` (width) value is multiplied by fx, and each
  `id="21"` (y) and `id="10"` (height) value by fy. The file is changed as text, so every other byte is the base
  file's;
- each PNG in the image zip is resized to (width × fx, height × fy) in RGBA with a Lanczos filter, and saved as a
  PNG. An image whose size does not change is copied as it is. The zip has the base's entries in the base's order,
  deflated, with a fixed time;
- results are rounded to the nearest whole number, halves to the even one, and a width or height is at least 1.

The 800×480 and 960×480 layouts are squeezed sideways and not taller, as the installer's are. The script reads
the base through `make_nbtpkg.py`'s checks, and refuses a base it does not know how to scale: a property id other
than 2, 3, 9, 10, 20, 21 and 23, a value to scale that is not a plain non-negative whole number, or an image zip
entry that is not a PNG named `<digits>.png`. It writes each app's folder whole, through a temporary folder
beside it.

It needs Pillow; the layouts here were made with Pillow 13.0.0 on Python 3.14. The release workflow never runs
it. After taking in a change to an upstream layout, regenerate and commit the result:

```sh
python3 -m pip install Pillow
python3 nbtapps/make_layouts.py                      # every app with "layouts": true
python3 nbtapps/make_layouts.py boostdeck mhd-suite  # or the ones named
python3 nbtapps/make_layouts.py --check
```

`--check` makes the layouts again in memory and compares them with what is committed, and writes nothing. It
compares `SOURCE` and the XML byte for byte, and the images by their size and decoded pixels, as PNG and zip bytes
can differ between zlib builds. It exits with 1 and names each difference: a file missing or extra, an XML file
that differs, an image entry missing, out of order, of another size or with other pixels. On the same machine, a
rerun gives the same bytes.

`tests/test_make_layouts.py` checks what is committed with the standard library only: for each app and size, the
XML differs from the base only in those values, each by the rule; the image zip has the base's entries in its
order, each of the size the rule gives; `SOURCE` matches the zip; nothing else is there; and `make_nbtpkg.py`'s
`read_layouts` accepts them. The rounding is tested without Pillow too. The tests that run the generator on
synthetic input (XML and images by the rule, copies of unchanged images, `--check`, the refusals, writing a folder
whole) need Pillow, and are skipped without it, as they are in the workflow.

### Sizes at which an app shows the base layout

An app's `base_layout_at` in `apps.json` lists generated sizes at which its package holds no layout and the
head unit shows the base one. BoostDeck and ShiftDeck have `["1440x540"]`. Upstream's issue #56 reports, from a
1440×540 head unit on NBTevo_Y24073J, that their 1440×540 layouts are wider than the part of the screen an app
gets, so the right-hand column is cut off; #75 and #77 look like the same fault. The generated layouts here are
the same files as the desktop installer's (checked against v0.7.3), so they would be cut off too. The 1280×480
layout is narrower and fits, with room left over. This has not been seen on a head unit: take the key out once
upstream's layout fits, or once a 1440×540 car shows that it does.

The layouts of such a size stay committed under `nbtapps/layouts/`, and `make_layouts.py` still makes and checks
them. `build` leaves them out of the archive and out of `@SIZES@`.

### What the archive holds

Every size the app has a layout for, the base size included, goes to `payload/<Folder>/layouts/<WxH>/`, and so to
`${var_plugins}/<Folder>/layouts/<WxH>/<Folder>.xml` and `..._ALL_Images.zip` on the head unit. Nothing is
placed at `${var_plugins}/<Folder>/<Folder>.xml` or `${var_plugins}/<Folder>/<Folder>_ALL_Images.zip`: the
`postInstall` hook makes those two files. They are in no package's record, so the agent never writes over them or
removes them.

### Hooks (the hook step)

- Templates are committed at `nbtapps/hooks/select-layout.sh.in` and `nbtapps/hooks/remove-layout.sh.in`.
- `build` fills in `@FOLDER@` (the plugin folder), `@BASE_SIZE@` (`1280x480`), `@SIZES@` (the sizes the package
  holds a layout for, joined with `|`, base first: `1280x480|800x480|960x480|1440x540|1920x720`, for a shell
  `case`), `@BASE_AT@` (the app's `base_layout_at` sizes joined the same way, or `none`, which is no size) and
  `@WRITTEN@` (the app's `written` paths with spaces between, or nothing). It refuses a template in which any
  other `@NAME@` is left. The results are `hooks/select-layout.sh` (`postInstall`) and `hooks/remove-layout.sh`
  (`preRemove`), each with a 30 second timeout.
- The agent runs a hook with its shell, from a folder of its own that holds the package's files (for
  `postInstall`) or its hooks (for `preRemove`), as root on a car. Use `$NBT_VAR_PLUGINS_DIR`, not the
  current folder, to find the plugin. It gives
  it `NBT_ACTION`, `NBT_PKG_ID`, `NBT_PKG_VERSION`, `NBT_OLD_VERSION` on an upgrade, and `NBT_VAR_PLUGINS_DIR`,
  the folder the plugins are in. See the NBTApps `headunit/README.md`.
- The scripts are written for the head unit's `/bin/ksh` (pdksh) and the tools a probe found there. They use
  builtins, `sed`, `cp`, `mv`, `rm` and `sleep`, and no `[[ ]]`, `$'...'`, arrays, `local` or `$((...))`.
  `tests/test_hooks.py` runs them under this machine's `sh` (`HOOK_SHELL` names another), with a fake
  `LayerManagerControl`, and fails on a command outside that list.
- `select-layout.sh` (`postInstall`, on an install and on every upgrade):
  1. Finds the plugin at `$NBT_VAR_PLUGINS_DIR/<Folder>`. With the variable unset or empty, or the base size's two
     files missing under `layouts/<base>/`, it says why and exits 1.
  2. Looks for `LayerManagerControl` along `PATH` (`/opt/sys/bin` on a car) and runs
     `LayerManagerControl get screen 0` in the background with `LD_LIBRARY_PATH=/opt/sys/lib:/usr/lib:/lib`, its
     output going to `.select-layout.screen` in the plugin folder. It checks once a second whether the command
     has ended, and after 5 seconds kills it (`kill -9`), so a hung query cannot run into the hook's 30 second
     timeout. A command still there 2 seconds after the kill is left running, the log says so, and the script
     goes on.
  3. Takes the first line of the output that matches, case aside,
     `resolution:\s*x\s*=\s*([0-9]{2,4}),\s*y\s*=\s*([0-9]{2,4})`, as upstream's `source/tools/capture_headunit.py`
     does, and makes `<x>x<y>` of it. The output of a command that had to be killed is not read.
  4. Uses that size if it is one of `@SIZES@` and both its files are under `layouts/<size>/`. Otherwise it uses
     the base size and prints one line saying what it saw: no `LayerManagerControl`, no resolution in its output
     (with its exit status if that was not 0), no answer within 5 seconds, a size with no layout, a size in
     `@BASE_AT@`, or a known size with a file missing. Not finding the size is never an error.
  5. Copies the two files to `.select-layout.xml` and `.select-layout.zip` beside the active files, then renames
     each over its active file, so a file the HMI has open is never written into. On an upgrade it first keeps a
     copy of the active `<Folder>.xml` as `.select-layout.xml.old`, to put back if the second rename fails. With
     `umask 022` the files are mode 0644.
  6. If a copy or a rename fails, it removes its temporary files and exits 1. On an install it also removes the
     two active files, so the agent's undo finds the plugin folder empty and removes it. On an upgrade the
     previous active files stay as they were.
  7. Removes `.select-layout.screen` and its other files on every way out, and prints one line naming the size
     it placed. It touches nothing else in the plugin folder.
- `remove-layout.sh` (`preRemove`) removes the two active files and any `.select-layout.*` file a stopped
  `select-layout.sh` left, and nothing else in the plugin folder, so an owner's `gateway-ip.txt` stays. Before
  that it removes the app's `written` files (see [Files a plugin writes](#files-a-plugin-writes)), and says which
  it removed. It exits 0 when the files are not there and when `NBT_VAR_PLUGINS_DIR` is not set (it says so, and
  still removes the `written` files), and 1 only when a file is still there after it tried. If it fails, the
  agent logs it and removes the package anyway. An upgrade does not run the old version's `preRemove`: the new
  version's `postInstall` writes over the active files instead, and what the app saved stays.
- If `postInstall` fails, the agent undoes the install: it removes the files it placed and the folders it made,
  a folder only if it is empty. If it times out, the agent kills the hook's whole process group.

### Files a plugin writes

A plugin may write files while it runs. The agent removes what a package placed and nothing else, so such a file
would stay on the head unit after the package is gone. An app's `written` in `apps.json` lists the files its
plugin writes outside its own folder, by their full paths, and `remove-layout.sh` removes them when the package
is removed, with the temporary files the plugin makes beside them (`<file>.XXXXXX`, six characters). It never
removes a folder. Should the agent ever run it for an upgrade (`NBT_ACTION=upgrade`), it leaves them.

BoostDeck and ShiftDeck save their colour, units and shift-light settings in
`/var/opt/hmi/data/ID5/BoostDeck.prefs` and `/var/opt/hmi/data/ID5/ShiftDeck.prefs` (four bytes each, see
`source/native/src/gauge_preferences.h`). The settings survive an upgrade and go with a removal.

Only an app with layouts has the hook, so `written` is refused for the others. `gateway-ip.txt` is not listed:
an owner writes it, the plugin only reads it, and it stays.

## Making a release

A release is made by the workflow `nbtpkg` (`.github/workflows/nbtpkg.yml`), started by hand in the Actions tab
with the app, the version and an optional note. It:

1. stops at once if `NBTAPPS_COMMIT`, the pinned NBTApps commit, is empty or not a full commit. It is the
   commit nbtapps-packages pins in its own workflows;
2. checks out this repo and NBTApps' `headunit/tools` and `headunit/spec` at that commit;
3. runs the tests, then `build --require-key` with the key from the `nbtpkg` environment's secret
   `NBTPKG_AUTHOR_KEY`;
4. if the release `<app>-v<version>` exists, compares its archive with the one just built: the same bytes end the
   run well, other bytes fail it. A published version never changes;
5. otherwise creates the release as a draft pre-release at the commit it built, with the archive and
   `nbtpkg-record.json`, and publishes it.

One release holds one package. The nbtapps-packages bot reads this repo's releases and proposes each new version
to the catalogue. A release marked as a pre-release on GitHub is proposed as a pre-release. To make a version
stable, take the pre-release mark off its GitHub release and open a pull request in nbtapps-packages that takes
`"channel": "prerelease"` out of its record. A package's first version is added to nbtapps-packages by hand,
with its listing and `author.pub`; the bot only follows packages that are there.

The bot reads the 30 newest releases. With eight packages in one repo, a package whose newest release has fallen
out of those is not followed until it has a new one.

## Taking in upstream's changes

Merge `upstream/main` into this branch. Nothing outside `nbtapps/` and `.github/workflows/nbtpkg.yml` is the
fork's own, so a merge should not conflict.

```sh
git fetch upstream
git merge upstream/main
python3 -m unittest discover -s nbtapps/tests
```

Then build each app. If upstream changed an app, its checks say what changed: a new file, another connector id, a
new worker, a path in a library or worker that is not known here, or a base layout that makes `SOURCE` stale.
Update `apps.json`, the layouts or this script to match, after reading the change. A package whose bytes changed
needs a new version number.

## Notes

- Seven of the libraries also read an optional `gateway-ip.txt` in their plugin folder, which an owner writes for
  a different diagnostic gateway. No package holds one. The agent leaves such a file alone, and the plugin folder
  stays after a remove while it is there.
- A head unit with an app installed by hand in the same folder refuses the package (`stock-mismatch`). The
  owner moves the hand-installed folder out of `Plugins` first.

## Not done yet

- The workflow has never run, so no release exists and the secret `NBTPKG_AUTHOR_KEY` in the `nbtpkg`
  environment has not signed anything yet.
- The agent release that knows `${var_plugins}`, and the phone app release that does. `requires_agent` is
  `>=1.6.0`, which is not released yet.
- The generated layouts have not been seen on a head unit, and the hook scripts have not run on one. The
  1440×540 choice for BoostDeck and ShiftDeck rests on upstream's issue reports alone.
- The connector ids in the catalogue's `hmi-ids.json`, and each package's first pull request in nbtapps-packages.
- No package checks the head unit's firmware. Upstream's desktop installer accepts the firmware and framework
  pairs in `source/docs/installer-profiles.json` only.
- The licence of the fork's own files is MIT, as upstream's source is. This is not confirmed yet.
